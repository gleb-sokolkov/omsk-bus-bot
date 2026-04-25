"""Планировщик уведомлений о прибытии транспорта."""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, date
from typing import Optional

from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from .api_client import fetch_routes, filter_by_start_stop
from .config import DEFAULT_CITY_SLUG
from .kudikina_client import search_routes as kudikina_search, KudikinaRoute
from .models import Trip, RouteInfo, _minutes_to_hhmm, _hhmm_to_minutes
from .schedule_enricher import enrich_routes
from .storage import TripStorage

logger = logging.getLogger(__name__)

# Ключ: (user_id, trip_id, schedule_time_minutes, дата) — чтобы не слать повторно
_sent_notifications: set[tuple[int, str, int, date]] = set()


@dataclass
class LockInfo:
    """Залоченный автобус — пользователь выбрал «поеду на этом»."""
    bus_number: str
    sched_min: int
    chat_id: int
    message_id: int  # id сообщения для edit


# Залоченные рейсы: (user_id, trip_id) -> LockInfo
_locked: dict[tuple[int, str], LockInfo] = {}

# Кеш маршрутов: (user_id, trip_id) -> [(bus_number, sched_min, body_text), ...]
_route_cache: dict[tuple[int, str], list[tuple[str, int, str]]] = {}


def _cleanup_sent_cache():
    """Удалить записи за прошлые дни."""
    today = date.today()
    to_remove = {key for key in _sent_notifications if key[3] != today}
    for key in to_remove:
        _sent_notifications.discard(key)


def _notification_key(user_id: int, trip_id: str, schedule_min: int) -> tuple:
    return (user_id, trip_id, schedule_min, date.today())


def lock_bus(user_id: int, trip_id: str, bus_number: str, sched_min: int,
             chat_id: int, message_id: int):
    """Залочить рейс на конкретный автобус."""
    _locked[(user_id, trip_id)] = LockInfo(
        bus_number=bus_number, sched_min=sched_min,
        chat_id=chat_id, message_id=message_id,
    )
    logger.info("Lock: user=%s trip=%s bus=%s at=%s",
                user_id, trip_id, bus_number, _minutes_to_hhmm(sched_min))


def unlock_trip(user_id: int, trip_id: str):
    """Разлочить рейс — вернуться к обычным уведомлениям."""
    removed = _locked.pop((user_id, trip_id), None)
    if removed:
        logger.info("Unlock: user=%s trip=%s", user_id, trip_id)


def get_lock(user_id: int, trip_id: str) -> Optional[LockInfo]:
    """Получить текущий lock для рейса."""
    return _locked.get((user_id, trip_id))


def get_cached_routes(user_id: int, trip_id: str) -> list[tuple[str, int, str]]:
    """Получить кешированные маршруты: [(bus_number, sched_min, body_text), ...]."""
    return _route_cache.get((user_id, trip_id), [])


def get_cached_body(user_id: int, trip_id: str, bus_number: str, sched_min: int) -> str:
    """Найти тело уведомления из кеша для конкретного автобуса."""
    for bn, sm, body in _route_cache.get((user_id, trip_id), []):
        if bn == bus_number and sm == sched_min:
            return body
    return ""


def _update_route_cache(
    user_id: int,
    trip_id: str,
    now_minutes: int,
    routes: list[RouteInfo],
    kd_routes: list[KudikinaRoute],
    trip_name: str = "",
    exit_minutes: int = 0,
    max_sched_min: int = 0,
) -> None:
    """Обновить кеш маршрутов — рейсы в пределах окна уведомлений, без дублей."""
    max_min = max_sched_min if max_sched_min > 0 else now_minutes + 60
    seen: set[tuple[str, int]] = set()
    result: list[tuple[str, int, str]] = []

    for route in routes:
        bus_number = _extract_bus_number(route)
        for sm in route.all_schedule_minutes():
            if now_minutes < sm <= max_min and (bus_number, sm) not in seen:
                seen.add((bus_number, sm))
                body = route.format_summary(
                    max_schedule_items=3, exit_minutes=exit_minutes,
                    target_boarding_min=sm,
                )
                result.append((bus_number, sm, body))

    for kd_route in kd_routes:
        for sm in kd_route.all_schedule_minutes():
            if now_minutes < sm <= max_min and (kd_route.number, sm) not in seen:
                seen.add((kd_route.number, sm))
                upcoming = kd_route.upcoming_times(count=3, from_minutes=sm)
                times_str = ", ".join(upcoming) if upcoming else ""
                lines = []
                if times_str:
                    lines.append(f"🚏 Расписание: {times_str}")
                lines.append(f"📍 {kd_route.direction}")
                body = "\n".join(lines)
                result.append((kd_route.number, sm, body))

    result.sort(key=lambda x: x[1])
    _route_cache[(user_id, trip_id)] = result


def _extract_bus_number(source) -> str:
    """Извлечь номер автобуса из источника (RouteInfo или KudikinaRoute)."""
    if isinstance(source, KudikinaRoute):
        return source.number
    segments = source.extract_passage_info()
    if segments:
        numbers = segments[0].get("bus_numbers", [])
        return numbers[0] if numbers else "?"
    return "?"


async def _fetch_and_filter(trip: Trip) -> list[RouteInfo]:
    """Запросить маршруты и применить фильтр по остановке."""
    routes = await fetch_routes(
        start_lat=trip.start_lat,
        start_lon=trip.start_lon,
        end_lat=trip.end_lat,
        end_lon=trip.end_lon,
        start_name=trip.start_address,
        end_name=trip.end_address,
    )
    if routes:
        routes = await enrich_routes(routes)
    if routes and trip.kudikina_start_stop:
        routes = filter_by_start_stop(routes, trip.kudikina_start_stop)
    return routes or []


async def _fetch_kudikina_routes(trip: Trip) -> list[KudikinaRoute]:
    """Запросить маршруты напрямую из kudikina (между сохранёнными остановками)."""
    if not trip.kudikina_start_stop or not trip.kudikina_end_stop:
        return []
    try:
        routes = await kudikina_search(
            DEFAULT_CITY_SLUG,
            trip.kudikina_start_stop,
            trip.kudikina_end_stop,
        )
        return routes or []
    except Exception as e:
        logger.warning("Kudikina direct search failed for trip %s: %s", trip.id, e)
        return []


# ── Клавиатуры ───────────────────────────────────────────────

def _ride_keyboard(
    trip_id: str,
    bus_number: str,
    upcoming: list[tuple[int, int]],
) -> InlineKeyboardMarkup:
    """Кнопки выбора рейса — по одной на каждое ближайшее время.

    upcoming: [(sched_min, minutes_until), ...]
    """
    buttons = []
    for sched_min, minutes_until in upcoming:
        time_str = _minutes_to_hhmm(sched_min)
        buttons.append([InlineKeyboardButton(
            text=f"🚌 Поеду на {time_str} (через {minutes_until} мин)",
            callback_data=f"ride:{trip_id}:{bus_number}:{sched_min}",
        )])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _locked_keyboard(trip_id: str) -> InlineKeyboardMarkup:
    """Кнопки для залоченного сообщения."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="↩️ Ко всем маршрутам",
            callback_data=f"unlock:{trip_id}",
        )],
    ])


def _departed_keyboard(
    trip_id: str,
    bus_number: str,
    next_sched_min: Optional[int],
) -> InlineKeyboardMarkup:
    """Кнопки после отъезда: следующий + ко всем."""
    buttons = []
    if next_sched_min is not None:
        next_time = _minutes_to_hhmm(next_sched_min)
        buttons.append([InlineKeyboardButton(
            text=f"📡 Следующий ({next_time})",
            callback_data=f"nextbus:{trip_id}:{bus_number}:{next_sched_min}",
        )])
    buttons.append([InlineKeyboardButton(
        text="↩️ Ко всем маршрутам",
        callback_data=f"unlock:{trip_id}",
    )])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# ── Форматирование уведомлений ───────────────────────────────

def _build_kudikina_notification_text(
    trip_name: str,
    kd_route: KudikinaRoute,
    minutes_until: int,
    header_emoji: str = "🔔",
    header_label: str = "Напоминание",
    exit_minutes: int = 0,
    target_boarding_min: Optional[int] = None,
) -> str:
    """Сформировать текст уведомления по данным kudikina."""
    upcoming = kd_route.upcoming_times(count=3, from_minutes=target_boarding_min)
    times_str = ", ".join(upcoming) if upcoming else ""
    exit_hint = f" (с учётом {exit_minutes} мин на выход)" if exit_minutes else ""
    header = (
        f"{header_emoji} {header_label}: рейс «{trip_name}»\n"
        f"{kd_route.transport_type} {kd_route.number} через {minutes_until} мин{exit_hint}\n"
    )
    lines = [header]
    if times_str:
        lines.append(f"🚏 Расписание: {times_str}")
    lines.append(f"📍 {kd_route.direction}")
    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:4000] + "\n\n... (сокращено)"
    return text


def _build_notification_text(
    trip_name: str,
    route: RouteInfo,
    minutes_until: int,
    header_emoji: str = "🔔",
    header_label: str = "Напоминание",
    exit_minutes: int = 0,
    target_boarding_min: Optional[int] = None,
) -> str:
    """Сформировать текст уведомления с полным описанием маршрута."""
    stop_name = route.start_stop_name or "остановка"
    exit_hint = f" (с учётом {exit_minutes} мин на выход)" if exit_minutes else ""
    header = (
        f"{header_emoji} {header_label}: рейс «{trip_name}»\n"
        f"Автобус на «{stop_name}» через {minutes_until} мин{exit_hint}\n"
        f"\n━━━ Маршрут ━━━\n"
    )
    text = header + route.format_summary(max_schedule_items=3, exit_minutes=exit_minutes, target_boarding_min=target_boarding_min)
    if len(text) > 4000:
        text = text[:4000] + "\n\n... (сокращено)"
    return text


def _build_locked_text(
    bus_number: str,
    sched_min: int,
    minutes_until: int,
    trip_name: str,
    exit_minutes: int = 0,
    body_text: str = "",
) -> str:
    """Текст залоченного сообщения с обратным отсчётом и подробностями."""
    time_str = _minutes_to_hhmm(sched_min)
    exit_hint = f" (с учётом {exit_minutes} мин на выход)" if exit_minutes else ""
    emoji = "⚠️" if exit_minutes and minutes_until <= exit_minutes else "🚌"
    text = (
        f"{emoji} Еду на: рейс «{trip_name}»\n"
        f"Автобус {bus_number} ({time_str}) через {minutes_until} мин{exit_hint}"
    )
    if body_text:
        text += f"\n\n{body_text}"
    if len(text) > 4000:
        text = text[:4000] + "\n... (сокращено)"
    return text


# ── Отправка обычного уведомления ────────────────────────────

async def _send_notification(
    bot: Bot,
    user_id: int,
    trip: Trip,
    source,
    minutes_until: int,
    sched_min: int,
    header_emoji: str = "🔔",
    header_label: str = "Напоминание",
    nkey_prefix: str = "",
) -> None:
    """Отправить уведомление с кнопкой «Поеду на этом»."""
    bus_number = _extract_bus_number(source)

    if isinstance(source, RouteInfo):
        text = _build_notification_text(
            trip.name, source, minutes_until,
            header_emoji=header_emoji, header_label=header_label,
            exit_minutes=trip.exit_minutes, target_boarding_min=sched_min,
        )
        nkey = _notification_key(user_id, f"{nkey_prefix}{trip.id}", sched_min)
    else:
        text = _build_kudikina_notification_text(
            trip.name, source, minutes_until,
            header_emoji=header_emoji, header_label=header_label,
            exit_minutes=trip.exit_minutes, target_boarding_min=sched_min,
        )
        nkey = _notification_key(user_id, f"{nkey_prefix}kd_{trip.id}", sched_min)

    # Собираем ближайшие рейсы этого автобуса для кнопок (в пределах окна уведомлений)
    now_min = sched_min - minutes_until
    # Определяем конец окна из настроек трипа
    if "go_" in nkey_prefix and trip.go_notify_to:
        _wt = _hhmm_to_minutes(trip.go_notify_to)
    else:
        _wt = _hhmm_to_minutes(trip.notify_to) if trip.notify_to else None
    _notify = trip.go_notify_minutes if "go_" in nkey_prefix else (trip.notify_minutes or 0)
    _lead = _notify + trip.exit_minutes
    max_min = (_wt + _lead + 10) if _wt is not None else (now_min + _lead + 30)
    all_mins = source.all_schedule_minutes()
    upcoming = [(sm, sm - now_min) for sm in sorted(all_mins) if now_min < sm <= max_min][:3]
    if not upcoming:
        upcoming = [(sched_min, minutes_until)]
    keyboard = _ride_keyboard(trip.id, bus_number, upcoming)

    try:
        await bot.send_message(user_id, text, reply_markup=keyboard)
        _sent_notifications.add(nkey)
        log_type = "Гоу" if "go_" in nkey_prefix else "Регулярное"
        logger.info(
            "%s уведомление: user=%s trip=%s bus=%s at=%s source=%s",
            log_type, user_id, trip.id, bus_number, _minutes_to_hhmm(sched_min),
            "kudikina" if isinstance(source, KudikinaRoute) else "2gis",
        )
    except Exception as e:
        logger.error("Ошибка отправки уведомления user=%s: %s", user_id, e)


# ── Поиск следующего автобуса ────────────────────────────────

def _find_next_sched_min(all_sched_minutes: list[int], current_sched_min: int) -> Optional[int]:
    """Найти следующее время в расписании после current_sched_min."""
    for m in sorted(all_sched_minutes):
        if m > current_sched_min:
            return m
    return None


# ── Проверка залоченного автобуса ─────────────────────────────

async def _check_locked_bus(
    bot: Bot,
    user_id: int,
    trip: Trip,
    now_minutes: int,
    routes: list[RouteInfo],
    kd_routes: list[KudikinaRoute],
) -> None:
    """Обновить обратный отсчёт для залоченного автобуса."""
    lk = (user_id, trip.id)
    lock = _locked.get(lk)
    if not lock:
        return

    minutes_until = lock.sched_min - now_minutes

    # Собираем все расписания для bus_number (для поиска следующего)
    all_sched: list[int] = []
    for route in routes:
        if _extract_bus_number(route) == lock.bus_number:
            all_sched.extend(route.all_schedule_minutes())
    for kd_route in kd_routes:
        if kd_route.number == lock.bus_number:
            all_sched.extend(kd_route.all_schedule_minutes())

    if minutes_until <= 0:
        # ── Автобус ушёл ──
        next_min = _find_next_sched_min(all_sched, lock.sched_min)
        keyboard = _departed_keyboard(trip.id, lock.bus_number, next_min)
        time_str = _minutes_to_hhmm(lock.sched_min)

        if next_min is not None:
            next_time = _minutes_to_hhmm(next_min)
            text = f"🔴 {lock.bus_number} ({time_str}) ушёл.\nСледующий — в {next_time}."
        else:
            text = f"🔴 {lock.bus_number} ({time_str}) ушёл.\nСледующих рейсов в расписании нет."

        try:
            await bot.edit_message_text(
                text, chat_id=lock.chat_id, message_id=lock.message_id,
                reply_markup=keyboard,
            )
        except Exception as e:
            logger.error("Ошибка edit departed user=%s: %s", user_id, e)

        unlock_trip(user_id, trip.id)

    else:
        # ── Обратный отсчёт ──
        body = get_cached_body(user_id, trip.id, lock.bus_number, lock.sched_min)
        text = _build_locked_text(
            lock.bus_number, lock.sched_min, minutes_until,
            trip.name, trip.exit_minutes, body_text=body,
        )
        keyboard = _locked_keyboard(trip.id)
        try:
            await bot.edit_message_text(
                text, chat_id=lock.chat_id, message_id=lock.message_id,
                reply_markup=keyboard,
            )
        except Exception as e:
            # MessageNotModified — текст не изменился, нормально
            if "message is not modified" not in str(e).lower():
                logger.error("Ошибка edit locked user=%s: %s", user_id, e)


# ── Регулярные уведомления (ежедневные, по расписанию) ────────

async def _check_trip_notifications(
    bot: Bot,
    user_id: int,
    trip: Trip,
    now_minutes: int,
) -> None:
    """Проверить расписание для регулярного уведомления."""
    if trip.notify_minutes is None:
        return

    # Проверяем временное окно
    win_from = _hhmm_to_minutes(trip.notify_from)
    win_to = _hhmm_to_minutes(trip.notify_to)
    if win_from is not None and win_to is not None:
        if not (win_from <= now_minutes <= win_to):
            return

    lead = trip.notify_minutes + trip.exit_minutes

    try:
        routes = await _fetch_and_filter(trip)
    except Exception as e:
        logger.warning("Ошибка запроса маршрутов для рейса %s: %s", trip.id, e)
        routes = []

    kd_routes = await _fetch_kudikina_routes(trip)

    # Лимит кеша: конец окна уведомлений + lead + 10
    max_sched = (win_to + lead + 10) if win_to is not None else (now_minutes + lead + 30)

    # Обновляем кеш маршрутов
    _update_route_cache(user_id, trip.id, now_minutes, routes, kd_routes,
                        trip_name=trip.name, exit_minutes=trip.exit_minutes,
                        max_sched_min=max_sched)

    # Если рейс залочен — только обратный отсчёт, без обычных уведомлений
    if get_lock(user_id, trip.id):
        await _check_locked_bus(bot, user_id, trip, now_minutes, routes, kd_routes)
        return

    # ── Обычные уведомления: окно [lead, lead+10], minutes_until > 0 ──
    best = None
    for route in routes:
        for sched_min in route.all_schedule_minutes():
            minutes_until = sched_min - now_minutes
            if minutes_until <= 0:
                continue
            key = _notification_key(user_id, trip.id, sched_min)
            if key in _sent_notifications:
                continue
            if lead <= minutes_until <= lead + 10:
                if best is None or minutes_until < best[0]:
                    best = (minutes_until, sched_min, route)

    for kd_route in kd_routes:
        for sched_min in kd_route.all_schedule_minutes():
            minutes_until = sched_min - now_minutes
            if minutes_until <= 0:
                continue
            key = _notification_key(user_id, f"kd_{trip.id}", sched_min)
            if key in _sent_notifications:
                continue
            if lead <= minutes_until <= lead + 10:
                if best is None or minutes_until < best[0]:
                    best = (minutes_until, sched_min, kd_route)

    if best:
        minutes_until, sched_min, source = best
        await _send_notification(bot, user_id, trip, source, minutes_until, sched_min)


# ── Гоу-уведомления (однодневные, оперативные) ───────────────

def _deactivate_go(trip: Trip, user_id: int, storage: TripStorage):
    """Отключить гоу-уведомление."""
    trip.go_notify_minutes = None
    trip.go_notify_from = None
    trip.go_notify_to = None
    trip.go_notify_date = None
    storage.update_trip(user_id, trip)


async def _check_go_notification(
    bot: Bot,
    user_id: int,
    trip: Trip,
    now_minutes: int,
    storage: TripStorage,
) -> None:
    """Проверить расписание для гоу-уведомления."""
    if trip.go_notify_minutes is None:
        return

    today_str = date.today().isoformat()
    if trip.go_notify_date and trip.go_notify_date != today_str:
        logger.info(
            "Гоу-уведомление устарело (создано %s): user=%s trip=%s",
            trip.go_notify_date, user_id, trip.id,
        )
        _deactivate_go(trip, user_id, storage)
        return

    win_from = _hhmm_to_minutes(trip.go_notify_from) if trip.go_notify_from else None
    win_to = _hhmm_to_minutes(trip.go_notify_to) if trip.go_notify_to else None

    if win_from is not None and win_to is not None:
        if now_minutes > win_to:
            old_from = trip.go_notify_from
            old_to = trip.go_notify_to
            logger.info(
                "Гоу-уведомление истекло: user=%s trip=%s окно %s–%s",
                user_id, trip.id, old_from, old_to,
            )
            _deactivate_go(trip, user_id, storage)
            try:
                await bot.send_message(
                    user_id,
                    f"⚪ Гоу-уведомление для «{trip.name}» отключено "
                    f"(окно {old_from}–{old_to} истекло).",
                )
            except Exception:
                pass
            return
        if now_minutes < win_from:
            return

    lead = trip.go_notify_minutes + trip.exit_minutes

    try:
        routes = await _fetch_and_filter(trip)
    except Exception as e:
        logger.warning("Ошибка запроса маршрутов (go) для рейса %s: %s", trip.id, e)
        routes = []

    kd_routes = await _fetch_kudikina_routes(trip)

    # Лимит кеша: конец окна + lead + 10
    max_sched = (win_to + lead + 10) if win_to is not None else (now_minutes + lead + 30)

    # Обновляем кеш маршрутов
    _update_route_cache(user_id, trip.id, now_minutes, routes, kd_routes,
                        trip_name=trip.name, exit_minutes=trip.exit_minutes,
                        max_sched_min=max_sched)

    # Если рейс залочен — только обратный отсчёт
    if get_lock(user_id, trip.id):
        await _check_locked_bus(bot, user_id, trip, now_minutes, routes, kd_routes)
        return

    # ── Обычные уведомления: окно [lead, lead+10], minutes_until > 0 ──
    best = None
    for route in routes:
        for sched_min in route.all_schedule_minutes():
            minutes_until = sched_min - now_minutes
            if minutes_until <= 0:
                continue
            key = _notification_key(user_id, f"go_{trip.id}", sched_min)
            if key in _sent_notifications:
                continue
            if lead <= minutes_until <= lead + 10:
                if best is None or minutes_until < best[0]:
                    best = (minutes_until, sched_min, route)

    for kd_route in kd_routes:
        for sched_min in kd_route.all_schedule_minutes():
            minutes_until = sched_min - now_minutes
            if minutes_until <= 0:
                continue
            key = _notification_key(user_id, f"go_kd_{trip.id}", sched_min)
            if key in _sent_notifications:
                continue
            if lead <= minutes_until <= lead + 10:
                if best is None or minutes_until < best[0]:
                    best = (minutes_until, sched_min, kd_route)

    if best:
        minutes_until, sched_min, source = best
        await _send_notification(
            bot, user_id, trip, source, minutes_until, sched_min,
            header_emoji="🚶", header_label="Пора на выход",
            nkey_prefix="go_",
        )


# ── Основной цикл ────────────────────────────────────────────

async def notification_loop(bot: Bot, storage: TripStorage, interval: int = 60):
    """Фоновый цикл проверки уведомлений."""
    logger.info("Планировщик уведомлений запущен (интервал %d сек)", interval)

    while True:
        try:
            await asyncio.sleep(interval)

            _cleanup_sent_cache()

            now = datetime.now()
            now_minutes = now.hour * 60 + now.minute

            all_data = storage._data
            for user_id_str, trips_data in all_data.items():
                user_id = int(user_id_str)
                for trip_dict in trips_data:
                    trip = Trip.from_dict(trip_dict)

                    # Гоу-уведомление приоритетнее
                    if trip.go_notify_minutes is not None:
                        await _check_go_notification(bot, user_id, trip, now_minutes, storage)
                    elif trip.notify_minutes is not None:
                        await _check_trip_notifications(bot, user_id, trip, now_minutes)

        except asyncio.CancelledError:
            logger.info("Планировщик уведомлений остановлен")
            break
        except Exception:
            logger.exception("Ошибка в цикле уведомлений")
