"""Планировщик уведомлений о прибытии транспорта."""

import asyncio
import logging
from datetime import datetime, date
from typing import Optional

from aiogram import Bot

from .api_client import fetch_routes, filter_by_start_stop
from .config import DEFAULT_CITY_SLUG, NOTIFY_TAIL_MINUTES
from .kudikina_client import search_routes as kudikina_search, KudikinaRoute
from .models import Trip, RouteInfo, _minutes_to_hhmm, _hhmm_to_minutes
from .schedule_enricher import enrich_routes
from .storage import TripStorage

logger = logging.getLogger(__name__)

# Ключ: (user_id, trip_id, schedule_time_minutes, дата) — чтобы не слать повторно
_sent_notifications: set[tuple[int, str, int, date]] = set()


def _cleanup_sent_cache():
    """Удалить записи за прошлые дни."""
    today = date.today()
    to_remove = {key for key in _sent_notifications if key[3] != today}
    for key in to_remove:
        _sent_notifications.discard(key)


def _notification_key(user_id: int, trip_id: str, schedule_min: int) -> tuple:
    return (user_id, trip_id, schedule_min, date.today())


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
        routes = await enrich_routes(routes)  # kudikina schedules overlay
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


def _build_kudikina_notification_text(
    trip_name: str,
    kd_route: KudikinaRoute,
    minutes_until: int,
    header_emoji: str = "🔔",
    header_label: str = "Напоминание",
    exit_minutes: int = 0,
    target_boarding_min: Optional[int] = None,
    is_urgent: bool = False,
) -> str:
    """Сформировать текст уведомления по данным kudikina."""
    if is_urgent:
        header_emoji = "⚠️"
        header_label = "Автобус скоро"
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
    is_urgent: bool = False,
) -> str:
    """Сформировать текст уведомления с полным описанием маршрута."""
    if is_urgent:
        header_emoji = "⚠️"
        header_label = "Автобус скоро"
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

    # lead_time = notify + exit (уведомить заранее с учётом времени на выход)
    lead = trip.notify_minutes + trip.exit_minutes
    tail = NOTIFY_TAIL_MINUTES  # расширение окна вниз для «опаздывающих» автобусов

    try:
        routes = await _fetch_and_filter(trip)
    except Exception as e:
        logger.warning("Ошибка запроса маршрутов для рейса %s: %s", trip.id, e)
        routes = []

    # Ищем ближайший подходящий рейс среди всех источников
    # best: (minutes_until, sched_min, source) где source — RouteInfo или KudikinaRoute
    best = None

    # 1) 2GIS маршруты
    for route in routes:
        schedule_times = route.all_schedule_minutes()
        for sched_min in schedule_times:
            minutes_until = sched_min - now_minutes
            is_tail = minutes_until < lead
            prefix = "tail_" if is_tail else ""
            key = _notification_key(user_id, f"{prefix}{trip.id}", sched_min)
            if key in _sent_notifications:
                continue
            if (lead - tail) <= minutes_until <= lead + 10:
                if best is None or minutes_until < best[0]:
                    best = (minutes_until, sched_min, route)

    # 2) Kudikina прямой поиск (если обе остановки заданы)
    kd_routes = await _fetch_kudikina_routes(trip)
    for kd_route in kd_routes:
        for sched_min in kd_route.all_schedule_minutes():
            minutes_until = sched_min - now_minutes
            is_tail = minutes_until < lead
            prefix = "tail_" if is_tail else ""
            key = _notification_key(user_id, f"{prefix}kd_{trip.id}", sched_min)
            if key in _sent_notifications:
                continue
            if (lead - tail) <= minutes_until <= lead + 10:
                if best is None or minutes_until < best[0]:
                    best = (minutes_until, sched_min, kd_route)

    if best:
        minutes_until, sched_min, source = best
        is_urgent = minutes_until < lead
        urgent_prefix = "tail_" if is_urgent else ""
        if isinstance(source, RouteInfo):
            text = _build_notification_text(trip.name, source, minutes_until, exit_minutes=trip.exit_minutes, target_boarding_min=sched_min, is_urgent=is_urgent)
            nkey = _notification_key(user_id, f"{urgent_prefix}{trip.id}", sched_min)
        else:
            text = _build_kudikina_notification_text(trip.name, source, minutes_until, exit_minutes=trip.exit_minutes, target_boarding_min=sched_min, is_urgent=is_urgent)
            nkey = _notification_key(user_id, f"{urgent_prefix}kd_{trip.id}", sched_min)
        try:
            await bot.send_message(user_id, text)
            _sent_notifications.add(nkey)
            logger.info(
                "Регулярное уведомление%s: user=%s trip=%s bus_at=%s source=%s",
                " (urgent)" if is_urgent else "",
                user_id, trip.id, _minutes_to_hhmm(sched_min),
                "kudikina" if isinstance(source, KudikinaRoute) else "2gis",
            )
        except Exception as e:
            logger.error("Ошибка отправки уведомления user=%s: %s", user_id, e)


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

    # Проверяем дату — гоу действует только в день создания
    today_str = date.today().isoformat()
    if trip.go_notify_date and trip.go_notify_date != today_str:
        logger.info(
            "Гоу-уведомление устарело (создано %s): user=%s trip=%s",
            trip.go_notify_date, user_id, trip.id,
        )
        _deactivate_go(trip, user_id, storage)
        return

    # Проверяем временное окно — если вышли за пределы, отключаем
    win_from = _hhmm_to_minutes(trip.go_notify_from) if trip.go_notify_from else None
    win_to = _hhmm_to_minutes(trip.go_notify_to) if trip.go_notify_to else None

    if win_from is not None and win_to is not None:
        if now_minutes > win_to:
            # Окно прошло — отключаем
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

    # lead_time = go_notify + exit
    lead = trip.go_notify_minutes + trip.exit_minutes
    tail = NOTIFY_TAIL_MINUTES

    try:
        routes = await _fetch_and_filter(trip)
    except Exception as e:
        logger.warning("Ошибка запроса маршрутов (go) для рейса %s: %s", trip.id, e)
        routes = []

    # Ищем ближайший подходящий рейс среди всех источников
    best = None

    # 1) 2GIS маршруты
    for route in routes:
        schedule_times = route.all_schedule_minutes()
        for sched_min in schedule_times:
            minutes_until = sched_min - now_minutes
            is_tail = minutes_until < lead
            prefix = "tail_" if is_tail else ""
            key = _notification_key(user_id, f"{prefix}go_{trip.id}", sched_min)
            if key in _sent_notifications:
                continue
            if (lead - tail) <= minutes_until <= lead + 10:
                if best is None or minutes_until < best[0]:
                    best = (minutes_until, sched_min, route)

    # 2) Kudikina прямой поиск
    kd_routes = await _fetch_kudikina_routes(trip)
    for kd_route in kd_routes:
        for sched_min in kd_route.all_schedule_minutes():
            minutes_until = sched_min - now_minutes
            is_tail = minutes_until < lead
            prefix = "tail_" if is_tail else ""
            key = _notification_key(user_id, f"{prefix}go_kd_{trip.id}", sched_min)
            if key in _sent_notifications:
                continue
            if (lead - tail) <= minutes_until <= lead + 10:
                if best is None or minutes_until < best[0]:
                    best = (minutes_until, sched_min, kd_route)

    if best:
        minutes_until, sched_min, source = best
        is_urgent = minutes_until < lead
        urgent_prefix = "tail_" if is_urgent else ""
        if isinstance(source, RouteInfo):
            text = _build_notification_text(
                trip.name, source, minutes_until,
                header_emoji="🚶",
                header_label="Пора на выход",
                exit_minutes=trip.exit_minutes,
                target_boarding_min=sched_min,
                is_urgent=is_urgent,
            )
            nkey = _notification_key(user_id, f"{urgent_prefix}go_{trip.id}", sched_min)
        else:
            text = _build_kudikina_notification_text(
                trip.name, source, minutes_until,
                header_emoji="🚶",
                header_label="Пора на выход",
                exit_minutes=trip.exit_minutes,
                target_boarding_min=sched_min,
                is_urgent=is_urgent,
            )
            nkey = _notification_key(user_id, f"{urgent_prefix}go_kd_{trip.id}", sched_min)
        try:
            await bot.send_message(user_id, text)
            _sent_notifications.add(nkey)
            logger.info(
                "Гоу-уведомление%s: user=%s trip=%s bus_at=%s source=%s",
                " (urgent)" if is_urgent else "",
                user_id, trip.id, _minutes_to_hhmm(sched_min),
                "kudikina" if isinstance(source, KudikinaRoute) else "2gis",
            )
        except Exception as e:
            logger.error("Ошибка отправки гоу-уведомления user=%s: %s", user_id, e)


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

                    # Гоу-уведомление приоритетнее — если активно, обычное пропускаем
                    if trip.go_notify_minutes is not None:
                        await _check_go_notification(bot, user_id, trip, now_minutes, storage)
                    elif trip.notify_minutes is not None:
                        await _check_trip_notifications(bot, user_id, trip, now_minutes)

        except asyncio.CancelledError:
            logger.info("Планировщик уведомлений остановлен")
            break
        except Exception:
            logger.exception("Ошибка в цикле уведомлений")
