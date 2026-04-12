"""Telegram-хендлеры бота."""

import logging
from typing import Optional

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)

from .api_client import fetch_routes
from .config import DEFAULT_CITY
from .geocoder import geocode, GeoResult
from .kudikina_client import search_routes as kudikina_search, CITY_SLUGS, DEFAULT_CITY_SLUG
from .models import Trip
from .schedule_enricher import enrich_routes
from .storage import TripStorage

logger = logging.getLogger(__name__)
router = Router()
storage = TripStorage()


# ── FSM-состояния ──────────────────────────────────────────────

class AddTripStates(StatesGroup):
    waiting_name = State()
    waiting_start_address = State()
    waiting_start_select = State()
    waiting_end_address = State()
    waiting_end_select = State()


class SetExitStates(StatesGroup):
    waiting_minutes = State()


class SetStopsStates(StatesGroup):
    waiting_start_input = State()   # user types stop name
    waiting_start_select = State()  # user picks from suggestions
    waiting_end_input = State()     # user types stop name
    waiting_end_select = State()    # user picks from suggestions


class EditTripStates(StatesGroup):
    waiting_field = State()        # выбор поля для редактирования
    waiting_name = State()         # ввод нового названия
    waiting_start_address = State()  # ввод нового адреса старта
    waiting_start_select = State()   # выбор из результатов геокодинга
    waiting_start_stop_keep = State()  # оставить/сбросить/заменить остановку старта
    waiting_new_stop_name = State()  # ввод новой остановки старта
    waiting_end_address = State()    # ввод нового адреса финиша
    waiting_end_select = State()     # выбор из результатов геокодинга


class SetNotifyStates(StatesGroup):
    waiting_minutes = State()
    waiting_time_window = State()


class GoNotifyStates(StatesGroup):
    waiting_minutes = State()


class KSearchStates(StatesGroup):
    waiting_city = State()
    waiting_from_stop = State()
    waiting_to_stop = State()


# ── Хелпер: создать клавиатуру выбора из результатов геокодинга ─

def _geo_keyboard(results: list[GeoResult], prefix: str) -> InlineKeyboardMarkup:
    """Inline-клавиатура для выбора адреса из результатов геокодинга.

    callback_data формат: {prefix}:{index}
    Координаты и адрес хранятся в FSM state data.
    """
    buttons = []
    for i, r in enumerate(results):
        buttons.append([
            InlineKeyboardButton(
                text=r.short_display(),
                callback_data=f"{prefix}:{i}",
            )
        ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


_CANCEL_BTN = [InlineKeyboardButton(text="↩️ Отмена", callback_data="inline_cancel")]


def _append_cancel(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    """Создать InlineKeyboardMarkup с кнопкой отмены в конце."""
    return InlineKeyboardMarkup(inline_keyboard=rows + [_CANCEL_BTN])


# ── /cancel — отмена текущей команды ──────────────────────────

@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    current = await state.get_state()
    if current is None:
        await message.answer("Нет активной команды для отмены.")
        return
    await state.clear()
    await message.answer("Команда отменена.")


@router.callback_query(F.data == "inline_cancel")
async def cb_inline_cancel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.answer("Отменено.")
    await callback.message.edit_text("Команда отменена.")


# ── /start, /help ──────────────────────────────────────────────

@router.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer(
        "Привет! Я бот для маршрутов общественного транспорта Омска (через 2GIS).\n\n"
        "Команды:\n"
        "/add — добавить новый рейс\n"
        "/trips — мои рейсы\n"
        "/edit — изменить рейс\n"
        "/delete — удалить рейс\n"
        "/setstops — настроить остановки (фильтр + расписание)\n\n"
        "/notify — ежедневные уведомления о прибытии\n"
        "/go — пора на выход (разовое уведомление)\n"
        "/setexit — время на выход из дома (мин)\n\n"
        "/route — получить маршрут для рейса\n"
        "/ksearch — расписание между остановками (kudikina.ru)\n\n"
        "/help — справка\n"
        "/cancel — отменить текущую команду"
    )


@router.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "Как пользоваться:\n\n"
        "📋 Рейсы:\n"
        "/add — добавить рейс (название + адреса)\n"
        "/trips — список рейсов\n"
        "/edit, /delete — изменить или удалить\n"
        "/setstops — настроить остановки (фильтр + расписание)\n\n"
        "🔔 Уведомления:\n"
        "/notify — ежедневные уведомления\n"
        "/go — разовое «пора на выход»\n"
        "/setexit — время на выход из дома\n\n"
        "🔎 Поиск:\n"
        "/route — маршрут для рейса\n"
        "/ksearch — расписание между остановками\n\n"
        "━━━ Примеры использования ━━━\n\n"
        "🚌 Ежедневная поездка на работу:\n"
        "Создайте рейс /add → настройте остановку старта /setstops "
        "(в выдаче останутся только маршруты от вашей остановки) → "
        "включите уведомления /notify за 10 мин в окне 07:00–08:00. "
        "Каждое утро бот напомнит о ближайшем автобусе.\n\n"
        "⏰ Опаздываете и нужно быстро узнать, когда автобус:\n"
        "Нажмите /go — бот пришлёт разовое уведомление "
        "о ближайшем автобусе с учётом времени на выход (/setexit).\n\n"
        "📍 Точное расписание из kudikina.ru:\n"
        "В /setstops задайте обе остановки (старт + финиш) — "
        "бот будет показывать расписание всех автобусов на этом участке "
        "и учитывать его в уведомлениях."
    )


# ── /add — добавление рейса ────────────────────────────────────

@router.message(Command("add"))
async def cmd_add(message: Message, state: FSMContext):
    await state.set_state(AddTripStates.waiting_name)
    await message.answer("Введите название рейса (например: Дом → Работа):")


@router.message(AddTripStates.waiting_name)
async def add_trip_name(message: Message, state: FSMContext):
    await state.update_data(name=message.text.strip())
    await state.set_state(AddTripStates.waiting_start_address)
    await message.answer("Введите адрес старта (например: ул. Ленина 25):")


@router.message(AddTripStates.waiting_start_address)
async def add_trip_start_address(message: Message, state: FSMContext):
    query = message.text.strip()
    try:
        results = await geocode(query, city_name=DEFAULT_CITY)
    except Exception as e:
        logger.exception("Ошибка геокодинга")
        await message.answer(f"Ошибка поиска адреса: {e}\nПопробуйте ещё раз:")
        return

    if not results:
        await message.answer("Адрес не найден. Попробуйте ввести более точный адрес:")
        return

    # Сохраняем результаты в state для последующего выбора
    await state.update_data(
        start_geo_results=[
            {"name": r.name, "full_address": r.full_address, "lat": r.lat, "lon": r.lon}
            for r in results
        ]
    )

    if len(results) == 1:
        # Единственный результат — берём сразу
        r = results[0]
        await state.update_data(
            start_address=r.full_address,
            start_lat=r.lat,
            start_lon=r.lon,
        )
        await state.set_state(AddTripStates.waiting_end_address)
        await message.answer(
            f"Старт: {r.full_address}\n\nТеперь введите адрес финиша:"
        )
    else:
        await state.set_state(AddTripStates.waiting_start_select)
        keyboard = _geo_keyboard(results, "geo_start")
        await message.answer("Выберите адрес старта:", reply_markup=keyboard)


@router.callback_query(F.data.startswith("geo_start:"))
async def cb_select_start(callback: CallbackQuery, state: FSMContext):
    idx = int(callback.data.split(":", 1)[1])
    data = await state.get_data()
    results = data.get("start_geo_results", [])

    if idx >= len(results):
        await callback.answer("Неверный выбор.", show_alert=True)
        return

    r = results[idx]
    await state.update_data(
        start_address=r["full_address"],
        start_lat=r["lat"],
        start_lon=r["lon"],
    )
    await callback.answer()
    await state.set_state(AddTripStates.waiting_end_address)
    await callback.message.answer(
        f"Старт: {r['full_address']}\n\nТеперь введите адрес финиша:"
    )


@router.message(AddTripStates.waiting_end_address)
async def add_trip_end_address(message: Message, state: FSMContext):
    query = message.text.strip()
    try:
        results = await geocode(query, city_name=DEFAULT_CITY)
    except Exception as e:
        logger.exception("Ошибка геокодинга")
        await message.answer(f"Ошибка поиска адреса: {e}\nПопробуйте ещё раз:")
        return

    if not results:
        await message.answer("Адрес не найден. Попробуйте ввести более точный адрес:")
        return

    await state.update_data(
        end_geo_results=[
            {"name": r.name, "full_address": r.full_address, "lat": r.lat, "lon": r.lon}
            for r in results
        ]
    )

    if len(results) == 1:
        r = results[0]
        await _finish_add_trip(message, state, r.full_address, r.lat, r.lon)
    else:
        await state.set_state(AddTripStates.waiting_end_select)
        keyboard = _geo_keyboard(results, "geo_end")
        await message.answer("Выберите адрес финиша:", reply_markup=keyboard)


@router.callback_query(F.data.startswith("geo_end:"))
async def cb_select_end(callback: CallbackQuery, state: FSMContext):
    idx = int(callback.data.split(":", 1)[1])
    data = await state.get_data()
    results = data.get("end_geo_results", [])

    if idx >= len(results):
        await callback.answer("Неверный выбор.", show_alert=True)
        return

    r = results[idx]
    await callback.answer()
    await _finish_add_trip(callback.message, state, r["full_address"], r["lat"], r["lon"])


async def _finish_add_trip(message: Message, state: FSMContext, end_address: str, end_lat: float, end_lon: float):
    """Завершение добавления рейса после выбора обоих адресов."""
    data = await state.get_data()

    trip = Trip(
        id="",
        name=data["name"],
        start_lat=data["start_lat"],
        start_lon=data["start_lon"],
        end_lat=end_lat,
        end_lon=end_lon,
        start_address=data["start_address"],
        end_address=end_address,
    )
    trip = storage.add_trip(message.chat.id, trip)
    await state.clear()
    await message.answer(
        f"Рейс «{trip.name}» сохранён!\n"
        f"📍 {trip.start_address}\n"
        f"🏁 {trip.end_address}\n"
        f"ID: {trip.id}\n\n"
        f"💡 Настройте остановки командой /setstops — "
        f"фильтр маршрутов и расписание kudikina.ru."
    )


# ── /trips — список рейсов ─────────────────────────────────────

@router.message(Command("trips"))
async def cmd_trips(message: Message):
    trips = storage.get_trips(message.from_user.id)
    if not trips:
        await message.answer("У вас нет сохранённых рейсов. Добавьте командой /add.")
        return

    lines = []
    for t in trips:
        if t.notify_minutes:
            exit_info = f"+{t.exit_minutes} на выход" if t.exit_minutes else ""
            notify_info = f" [🔔 за {t.notify_minutes}{exit_info} мин {t.notify_from}–{t.notify_to}]"
        else:
            notify_info = ""
        lines.append(
            f"• <b>{t.name}</b> (ID: {t.id}){notify_info}\n"
            f"  📍 {t.start_address}\n"
            f"  🏁 {t.end_address}"
        )
    await message.answer("\n\n".join(lines), parse_mode="HTML")


# ── /route — получить маршрут ──────────────────────────────────

@router.message(Command("route"))
async def cmd_route(message: Message):
    trips = storage.get_trips(message.from_user.id)
    if not trips:
        await message.answer("Нет сохранённых рейсов. Добавьте командой /add.")
        return

    keyboard = _append_cancel([
        [InlineKeyboardButton(text=t.name, callback_data=f"route:{t.id}")]
        for t in trips
    ])
    await message.answer("Выберите рейс:", reply_markup=keyboard)


@router.callback_query(F.data.startswith("route:"))
async def cb_route(callback: CallbackQuery):
    trip_id = callback.data.split(":", 1)[1]
    trip = storage.get_trip(callback.from_user.id, trip_id)
    if not trip:
        await callback.answer("Рейс не найден.", show_alert=True)
        return

    await callback.answer()
    await callback.message.answer(f"Ищу маршруты для «{trip.name}»...")

    try:
        routes = await fetch_routes(
            start_lat=trip.start_lat,
            start_lon=trip.start_lon,
            end_lat=trip.end_lat,
            end_lon=trip.end_lon,
            start_name=trip.start_address,
            end_name=trip.end_address,
        )
    except Exception as e:
        logger.exception("Ошибка при запросе маршрутов")
        await callback.message.answer(f"Ошибка: {e}")
        return

    if not routes:
        await callback.message.answer("Маршруты не найдены.")
        return

    # Обогащаем расписание данными из kudikina.ru
    routes = await enrich_routes(routes)

    # Фильтруем по остановке старта, если задана
    if trip.kudikina_start_stop:
        from .api_client import filter_by_start_stop
        routes = filter_by_start_stop(routes, trip.kudikina_start_stop)

    header = f"Маршруты для «{trip.name}»:\n"
    parts = [header]
    for i, route in enumerate(routes, 1):
        parts.append(f"\n━━━ Вариант {i} ━━━")
        parts.append(route.format_summary(exit_minutes=trip.exit_minutes))

    text = "\n".join(parts)
    if len(text) > 4000:
        text = text[:4000] + "\n\n... (сокращено)"

    await callback.message.answer(text)

    # Дополнительный поиск через kudikina, если выбраны обе остановки
    if trip.kudikina_start_stop and trip.kudikina_end_stop:
        await _send_kudikina_extra(callback.message, trip)


async def _send_kudikina_extra(message: Message, trip: Trip):
    """Запросить kudikina напрямую по остановкам и показать доп. расписание."""
    from .kudikina_client import search_routes as kudikina_search
    from .config import DEFAULT_CITY_SLUG

    try:
        kd_routes = await kudikina_search(
            DEFAULT_CITY_SLUG,
            trip.kudikina_start_stop,
            trip.kudikina_end_stop,
        )
    except Exception as e:
        logger.warning("Kudikina доп. поиск не удался: %s", e)
        return

    if not kd_routes:
        return

    lines = [
        f"🔎 Kudikina: {trip.kudikina_start_stop} → {trip.kudikina_end_stop}\n"
    ]
    for kr in kd_routes[:5]:
        upcoming = kr.upcoming_times(count=3)
        times_str = ", ".join(upcoming) if upcoming else "нет данных"
        lines.append(f"  🚌 {kr.transport_type} {kr.number}: {times_str}")

    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:4000] + "\n\n... (сокращено)"

    await message.answer(text)


# ── /setstops — настроить остановки (фильтр + расписание) ─────────

@router.message(Command("setstops"))
async def cmd_setstops(message: Message, state: FSMContext):
    trips = storage.get_trips(message.from_user.id)
    if not trips:
        await message.answer("Нет сохранённых рейсов.")
        return

    keyboard = _append_cancel([
        [InlineKeyboardButton(
            text=_setstops_label(t),
            callback_data=f"setstops:{t.id}",
        )]
        for t in trips
    ])
    await message.answer(
        "Выберите рейс для настройки остановок.\n\n"
        "📍 Остановка старта — фильтрует маршруты 2GIS: "
        "в выдаче останутся только те, что начинаются от неё.\n"
        "📍+🏁 Обе остановки — дополнительно активируют расписание из kudikina.ru.",
        reply_markup=keyboard,
    )


def _setstops_label(t: Trip) -> str:
    parts = [t.name]
    if t.kudikina_start_stop and t.kudikina_end_stop:
        parts.append(f"({t.kudikina_start_stop} → {t.kudikina_end_stop})")
    elif t.kudikina_start_stop or t.kudikina_end_stop:
        parts.append("(частично)")
    return " ".join(parts)


@router.callback_query(F.data.startswith("setstops:"))
async def cb_setstops(callback: CallbackQuery, state: FSMContext):
    trip_id = callback.data.split(":", 1)[1]
    trip = storage.get_trip(callback.from_user.id, trip_id)
    if not trip:
        await callback.answer("Рейс не найден.", show_alert=True)
        return

    await callback.answer()
    await state.update_data(setstops_trip_id=trip_id)
    await state.set_state(SetStopsStates.waiting_start_input)

    current = trip.kudikina_start_stop or "не задана"
    buttons = [
        [InlineKeyboardButton(text="⏭ Пропустить — перейти к финишу", callback_data="kstart:skip")],
        [InlineKeyboardButton(text="🗑 Сбросить обе остановки", callback_data="kstart:reset")],
    ]
    await callback.message.answer(
        f"Рейс: «{trip.name}»\n"
        f"Текущая остановка старта: {current}\n\n"
        f"📍 Введите название остановки СТАРТА:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@router.message(SetStopsStates.waiting_start_input)
async def setstops_start_input(message: Message, state: FSMContext):
    from .api_client import suggest_stops
    from .config import TWOGIS_REGION_ID

    text = message.text.strip()

    # Пробуем автодополнение
    suggestions = await suggest_stops(text, region_id=TWOGIS_REGION_ID)
    if suggestions:
        buttons = [
            [InlineKeyboardButton(text=s, callback_data=f"kstart:{i}")]
            for i, s in enumerate(suggestions)
        ]
        buttons.append([InlineKeyboardButton(text=f"Ввести как есть: «{text}»", callback_data="kstart:asis")])
        buttons.append([InlineKeyboardButton(text="🔄 Ввести другое", callback_data="kstart:retry")])
        buttons.append([InlineKeyboardButton(text="❌ Отмена", callback_data="inline_cancel")])
        await state.update_data(kstart_input=text, kstart_suggestions=suggestions)
        await state.set_state(SetStopsStates.waiting_start_select)
        await message.answer(
            f"📍 Подсказки для «{text}»:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        )
    else:
        # Нет подсказок — предлагаем повторить или сохранить как есть
        buttons = [
            [InlineKeyboardButton(text=f"Сохранить как есть: «{text}»", callback_data="kstart:asis")],
            [InlineKeyboardButton(text="🔄 Попробовать другое название", callback_data="kstart:retry")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="inline_cancel")],
        ]
        await state.update_data(kstart_input=text)
        await state.set_state(SetStopsStates.waiting_start_select)
        await message.answer(
            f"Подсказок для «{text}» не найдено.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        )


@router.callback_query(F.data == "kstart:skip", SetStopsStates.waiting_start_input)
async def setstops_start_skip(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await _go_to_end_stop(callback.message, state)


@router.callback_query(F.data == "kstart:reset", SetStopsStates.waiting_start_input)
async def setstops_reset(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    trip = storage.get_trip(callback.from_user.id, data.get("setstops_trip_id"))
    if trip:
        trip.kudikina_start_stop = None
        trip.kudikina_end_stop = None
        storage.update_trip(callback.from_user.id, trip)
    await callback.answer()
    await state.clear()
    await callback.message.edit_text("Обе остановки сброшены. Kudikina-поиск деактивирован.")


@router.callback_query(F.data.startswith("kstart:"), SetStopsStates.waiting_start_select)
async def setstops_start_select(callback: CallbackQuery, state: FSMContext):
    value = callback.data.split(":", 1)[1]
    data = await state.get_data()

    if value == "retry":
        await callback.answer()
        await state.set_state(SetStopsStates.waiting_start_input)
        await callback.message.edit_text("📍 Введите название остановки СТАРТА заново:")
        return

    if value == "asis":
        name = data.get("kstart_input", "")
    else:
        idx = int(value)
        suggestions = data.get("kstart_suggestions", [])
        if idx >= len(suggestions):
            await callback.answer("Неверный выбор.", show_alert=True)
            return
        name = suggestions[idx]

    await callback.answer()
    await _save_start_stop(callback.message, state, name)


async def _save_start_stop(message: Message, state: FSMContext, name: str):
    """Сохранить остановку старта и перейти к финишу."""
    data = await state.get_data()
    trip_id = data.get("setstops_trip_id")
    user_id = message.chat.id
    trip = storage.get_trip(user_id, trip_id)
    if trip:
        trip.kudikina_start_stop = name
        storage.update_trip(user_id, trip)

    await message.answer(f"✅ Остановка старта: {name}")
    await _go_to_end_stop(message, state)


async def _go_to_end_stop(message: Message, state: FSMContext):
    """Перейти к этапу ввода остановки финиша."""
    data = await state.get_data()
    trip_id = data.get("setstops_trip_id")
    user_id = message.chat.id
    trip = storage.get_trip(user_id, trip_id)
    current_end = trip.kudikina_end_stop or "не задана" if trip else "не задана"

    await state.set_state(SetStopsStates.waiting_end_input)
    buttons = [[InlineKeyboardButton(text="⏭ Пропустить — завершить", callback_data="kend:skip")]]
    await message.answer(
        f"Текущая остановка финиша: {current_end}\n\n"
        f"🏁 Введите название остановки ФИНИША:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@router.message(SetStopsStates.waiting_end_input)
async def setstops_end_input(message: Message, state: FSMContext):
    from .api_client import suggest_stops
    from .config import TWOGIS_REGION_ID

    text = message.text.strip()

    suggestions = await suggest_stops(text, region_id=TWOGIS_REGION_ID)
    if suggestions:
        buttons = [
            [InlineKeyboardButton(text=s, callback_data=f"kend:{i}")]
            for i, s in enumerate(suggestions)
        ]
        buttons.append([InlineKeyboardButton(text=f"Ввести как есть: «{text}»", callback_data="kend:asis")])
        buttons.append([InlineKeyboardButton(text="🔄 Ввести другое", callback_data="kend:retry")])
        buttons.append([InlineKeyboardButton(text="❌ Отмена", callback_data="inline_cancel")])
        await state.update_data(kend_input=text, kend_suggestions=suggestions)
        await state.set_state(SetStopsStates.waiting_end_select)
        await message.answer(
            f"🏁 Подсказки для «{text}»:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        )
    else:
        buttons = [
            [InlineKeyboardButton(text=f"Сохранить как есть: «{text}»", callback_data="kend:asis")],
            [InlineKeyboardButton(text="🔄 Попробовать другое название", callback_data="kend:retry")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="inline_cancel")],
        ]
        await state.update_data(kend_input=text)
        await state.set_state(SetStopsStates.waiting_end_select)
        await message.answer(
            f"Подсказок для «{text}» не найдено.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        )


@router.callback_query(F.data == "kend:skip", SetStopsStates.waiting_end_input)
async def setstops_end_skip(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    trip_id = data.get("setstops_trip_id")
    trip = storage.get_trip(callback.from_user.id, trip_id)
    await callback.answer()
    await state.clear()
    await _show_setstops_result(callback.message, trip)


@router.callback_query(F.data.startswith("kend:"), SetStopsStates.waiting_end_select)
async def setstops_end_select(callback: CallbackQuery, state: FSMContext):
    value = callback.data.split(":", 1)[1]
    data = await state.get_data()

    if value == "retry":
        await callback.answer()
        await state.set_state(SetStopsStates.waiting_end_input)
        await callback.message.edit_text("🏁 Введите название остановки ФИНИША заново:")
        return

    if value == "asis":
        name = data.get("kend_input", "")
    else:
        idx = int(value)
        suggestions = data.get("kend_suggestions", [])
        if idx >= len(suggestions):
            await callback.answer("Неверный выбор.", show_alert=True)
            return
        name = suggestions[idx]

    await callback.answer()
    await _save_end_stop(callback.message, state, name)


async def _save_end_stop(message: Message, state: FSMContext, name: str):
    """Сохранить остановку финиша и показать результат."""
    data = await state.get_data()
    trip_id = data.get("setstops_trip_id")
    user_id = message.chat.id
    trip = storage.get_trip(user_id, trip_id)
    if trip:
        trip.kudikina_end_stop = name
        storage.update_trip(user_id, trip)

    await state.clear()
    await _show_setstops_result(message, trip)


async def _show_setstops_result(message: Message, trip: Optional[Trip]):
    """Показать итог настройки остановок."""
    if not trip:
        await message.answer("Рейс не найден.")
        return

    # Финиш без старта бесполезен — сбрасываем
    if trip.kudikina_end_stop and not trip.kudikina_start_stop:
        trip.kudikina_end_stop = None
        storage.update_trip(message.chat.id, trip)

    start = trip.kudikina_start_stop or "—"
    end = trip.kudikina_end_stop or "—"

    if trip.kudikina_start_stop and trip.kudikina_end_stop:
        status = "✅ Kudikina-расписание + фильтрация маршрутов"
    elif trip.kudikina_start_stop:
        status = "✅ Фильтрация маршрутов по остановке старта"
    else:
        status = "⚠️ Остановки не заданы"

    await message.answer(
        f"Рейс: «{trip.name}»\n"
        f"📍 Остановка старта: {start}\n"
        f"🏁 Остановка финиша: {end}\n\n"
        f"{status}"
    )


# ── /setexit — задать время на выход из дома ────────────────────

@router.message(Command("setexit"))
async def cmd_setexit(message: Message, state: FSMContext):
    trips = storage.get_trips(message.from_user.id)
    if not trips:
        await message.answer("Нет сохранённых рейсов.")
        return

    keyboard = _append_cancel([
        [InlineKeyboardButton(
            text=f"{t.name} ({t.exit_minutes} мин)" if t.exit_minutes else t.name,
            callback_data=f"setexit:{t.id}",
        )]
        for t in trips
    ])
    await message.answer(
        "Выберите рейс, для которого хотите задать время на выход из дома:",
        reply_markup=keyboard,
    )


@router.callback_query(F.data.startswith("setexit:"))
async def cb_setexit(callback: CallbackQuery, state: FSMContext):
    trip_id = callback.data.split(":", 1)[1]
    trip = storage.get_trip(callback.from_user.id, trip_id)
    if not trip:
        await callback.answer("Рейс не найден.", show_alert=True)
        return

    await callback.answer()
    await state.update_data(setexit_trip_id=trip_id)
    await state.set_state(SetExitStates.waiting_minutes)

    current = f"{trip.exit_minutes} мин" if trip.exit_minutes else "не задано"
    await callback.message.answer(
        f"Рейс: «{trip.name}»\n"
        f"Текущее время на выход: {current}\n\n"
        f"Введите количество минут на выход из дома (спуск с этажа и т.д.)\n"
        f"или «0» чтобы сбросить:"
    )


@router.message(SetExitStates.waiting_minutes)
async def setexit_minutes(message: Message, state: FSMContext):
    data = await state.get_data()
    trip_id = data.get("setexit_trip_id")
    trip = storage.get_trip(message.from_user.id, trip_id)
    if not trip:
        await message.answer("Рейс не найден.")
        await state.clear()
        return

    text = message.text.strip()
    try:
        minutes = int(text)
    except ValueError:
        await message.answer("Введите число минут (например: 5):")
        return

    if minutes < 0:
        await message.answer("Число не может быть отрицательным. Введите ещё раз:")
        return

    trip.exit_minutes = minutes
    storage.update_trip(message.from_user.id, trip)
    await state.clear()

    if minutes > 0:
        await message.answer(
            f"Время на выход для «{trip.name}»: {minutes} мин.\n"
            f"Это будет учтено в расчёте времени выхода из дома."
        )
    else:
        await message.answer(f"Время на выход для «{trip.name}» сброшено.")


# ── /notify — настроить уведомления ────────────────────────────

@router.message(Command("notify"))
async def cmd_notify(message: Message, state: FSMContext):
    trips = storage.get_trips(message.from_user.id)
    if not trips:
        await message.answer("Нет сохранённых рейсов.")
        return

    keyboard = _append_cancel([
        [InlineKeyboardButton(
            text=f"{'🔔' if t.notify_minutes else '🔕'} {t.name}",
            callback_data=f"notify:{t.id}",
        )]
        for t in trips
    ])
    await message.answer(
        "Выберите рейс для настройки уведомлений:",
        reply_markup=keyboard,
    )


@router.callback_query(F.data.startswith("notify:"))
async def cb_notify(callback: CallbackQuery, state: FSMContext):
    trip_id = callback.data.split(":", 1)[1]
    trip = storage.get_trip(callback.from_user.id, trip_id)
    if not trip:
        await callback.answer("Рейс не найден.", show_alert=True)
        return

    await callback.answer()
    await state.update_data(notify_trip_id=trip_id)
    await state.set_state(SetNotifyStates.waiting_minutes)

    if trip.notify_minutes:
        exit_info = f" + {trip.exit_minutes} на выход" if trip.exit_minutes else ""
        current = f"за {trip.notify_minutes}{exit_info} мин, окно {trip.notify_from}–{trip.notify_to}"
    else:
        current = "выключены"

    exit_note = f"\n💡 Время на выход ({trip.exit_minutes} мин) будет добавлено автоматически." if trip.exit_minutes else ""
    await callback.message.answer(
        f"Рейс: «{trip.name}»\n"
        f"Уведомления: {current}\n\n"
        f"За сколько минут до прибытия автобуса вас уведомить?\n"
        f"Введите число (например: 10) или «0» чтобы выключить:{exit_note}"
    )


@router.message(SetNotifyStates.waiting_minutes)
async def set_notify_minutes(message: Message, state: FSMContext):
    data = await state.get_data()
    trip_id = data.get("notify_trip_id")
    trip = storage.get_trip(message.from_user.id, trip_id)
    if not trip:
        await message.answer("Рейс не найден.")
        await state.clear()
        return

    text = message.text.strip()
    try:
        minutes = int(text)
    except ValueError:
        await message.answer("Введите число минут (например: 10) или 0 для отключения:")
        return

    if minutes < 0:
        await message.answer("Число должно быть положительным. Введите ещё раз:")
        return

    if minutes == 0:
        trip.notify_minutes = None
        trip.notify_from = "07:00"
        trip.notify_to = "08:00"
        storage.update_trip(message.from_user.id, trip)
        await message.answer(f"🔕 Уведомления для «{trip.name}» выключены.")
        await state.clear()
    else:
        await state.update_data(notify_minutes_value=minutes)
        await state.set_state(SetNotifyStates.waiting_time_window)
        await message.answer(
            f"В какое время уведомлять?\n"
            f"Текущее окно: {trip.notify_from}–{trip.notify_to}\n\n"
            f"Введите промежуток в формате ЧЧ:ММ-ЧЧ:ММ (например: 07:00-08:00)\n"
            f"или «-» чтобы оставить текущее:"
        )


import re as _re

@router.message(SetNotifyStates.waiting_time_window)
async def set_notify_window(message: Message, state: FSMContext):
    data = await state.get_data()
    trip_id = data.get("notify_trip_id")
    minutes = data.get("notify_minutes_value")
    trip = storage.get_trip(message.from_user.id, trip_id)
    if not trip:
        await message.answer("Рейс не найден.")
        await state.clear()
        return

    text = message.text.strip()

    if text == "-":
        # Оставить текущее окно
        trip.notify_minutes = minutes
        storage.update_trip(message.from_user.id, trip)
        exit_hint = f" + {trip.exit_minutes} мин на выход = {minutes + trip.exit_minutes} мин" if trip.exit_minutes else ""
        await message.answer(
            f"🔔 Уведомления для «{trip.name}» включены!\n"
            f"Напомню за {minutes} мин до автобуса{exit_hint} в промежутке {trip.notify_from}–{trip.notify_to}."
        )
        await state.clear()
        return

    # Парсим формат HH:MM-HH:MM
    match = _re.match(r"(\d{1,2}:\d{2})\s*[-–]\s*(\d{1,2}:\d{2})", text)
    if not match:
        await message.answer(
            "Неверный формат. Введите промежуток ЧЧ:ММ-ЧЧ:ММ (например: 07:00-08:00)\n"
            "или «-» для уведомлений в любое время:"
        )
        return

    time_from = match.group(1)
    time_to = match.group(2)

    # Валидация
    from .models import _hhmm_to_minutes
    from_min = _hhmm_to_minutes(time_from)
    to_min = _hhmm_to_minutes(time_to)

    if from_min is None or to_min is None:
        await message.answer("Неверный формат времени. Попробуйте ещё раз:")
        return

    if to_min <= from_min:
        await message.answer("Время окончания должно быть позже времени начала. Попробуйте ещё раз:")
        return

    if to_min - from_min > 60:
        await message.answer("Максимальный промежуток — 1 час. Попробуйте ещё раз:")
        return

    trip.notify_minutes = minutes
    trip.notify_from = time_from
    trip.notify_to = time_to
    storage.update_trip(message.from_user.id, trip)
    exit_hint = f" + {trip.exit_minutes} мин на выход = {minutes + trip.exit_minutes} мин" if trip.exit_minutes else ""
    await message.answer(
        f"🔔 Уведомления для «{trip.name}» включены!\n"
        f"Напомню за {minutes} мин до автобуса{exit_hint} в промежутке {time_from}–{time_to}."
    )
    await state.clear()


# ── /edit — изменить рейс ──────────────────────────────────────

@router.message(Command("edit"))
async def cmd_edit(message: Message, state: FSMContext):
    trips = storage.get_trips(message.from_user.id)
    if not trips:
        await message.answer("Нет сохранённых рейсов.")
        return

    keyboard = _append_cancel([
        [InlineKeyboardButton(text=t.name, callback_data=f"edit:{t.id}")]
        for t in trips
    ])
    await message.answer("Выберите рейс для изменения:", reply_markup=keyboard)


@router.callback_query(F.data.startswith("edit:"))
async def cb_edit(callback: CallbackQuery, state: FSMContext):
    trip_id = callback.data.split(":", 1)[1]
    trip = storage.get_trip(callback.from_user.id, trip_id)
    if not trip:
        await callback.answer("Рейс не найден.", show_alert=True)
        return

    await callback.answer()
    await state.update_data(edit_trip_id=trip_id)
    await state.set_state(EditTripStates.waiting_field)

    keyboard = _append_cancel([
        [InlineKeyboardButton(text="✏️ Название", callback_data="editfield:name")],
        [InlineKeyboardButton(text="📍 Адрес старта", callback_data="editfield:start")],
        [InlineKeyboardButton(text="🏁 Адрес финиша", callback_data="editfield:end")],
    ])
    await callback.message.answer(
        f"Рейс: «{trip.name}»\n"
        f"📍 {trip.start_address}\n"
        f"🏁 {trip.end_address}\n\n"
        f"Что изменить?",
        reply_markup=keyboard,
    )


# ── Выбор поля ────────────────────────────────────────────────

@router.callback_query(F.data == "editfield:name", EditTripStates.waiting_field)
async def cb_edit_name(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(EditTripStates.waiting_name)
    await callback.message.answer("Введите новое название рейса:")


@router.callback_query(F.data == "editfield:start", EditTripStates.waiting_field)
async def cb_edit_start(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(EditTripStates.waiting_start_address)
    await callback.message.answer("Введите новый адрес старта:")


@router.callback_query(F.data == "editfield:end", EditTripStates.waiting_field)
async def cb_edit_end(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(EditTripStates.waiting_end_address)
    await callback.message.answer("Введите новый адрес финиша:")


# ── Изменение названия ────────────────────────────────────────

@router.message(EditTripStates.waiting_name)
async def edit_trip_name(message: Message, state: FSMContext):
    data = await state.get_data()
    trip = storage.get_trip(message.from_user.id, data["edit_trip_id"])
    if not trip:
        await message.answer("Рейс не найден.")
        await state.clear()
        return

    old_name = trip.name
    trip.name = message.text.strip()
    storage.update_trip(message.from_user.id, trip)
    await state.clear()
    await message.answer(f"Название изменено: «{old_name}» → «{trip.name}»")


# ── Изменение адреса старта ───────────────────────────────────

@router.message(EditTripStates.waiting_start_address)
async def edit_trip_start_address(message: Message, state: FSMContext):
    query = message.text.strip()
    try:
        results = await geocode(query, city_name=DEFAULT_CITY)
    except Exception as e:
        logger.exception("Ошибка геокодинга")
        await message.answer(f"Ошибка поиска адреса: {e}\nПопробуйте ещё раз:")
        return

    if not results:
        await message.answer("Адрес не найден. Попробуйте ввести более точный адрес:")
        return

    await state.update_data(
        edit_start_geo_results=[
            {"name": r.name, "full_address": r.full_address, "lat": r.lat, "lon": r.lon}
            for r in results
        ]
    )

    if len(results) == 1:
        r = results[0]
        await state.update_data(
            edit_new_start_address=r.full_address,
            edit_new_start_lat=r.lat,
            edit_new_start_lon=r.lon,
        )
        await _maybe_ask_about_stop(message, state)
    else:
        await state.set_state(EditTripStates.waiting_start_select)
        keyboard = _geo_keyboard(results, "editgeo_start")
        await message.answer("Выберите адрес старта:", reply_markup=keyboard)


@router.callback_query(F.data.startswith("editgeo_start:"))
async def cb_edit_select_start(callback: CallbackQuery, state: FSMContext):
    idx = int(callback.data.split(":", 1)[1])
    data = await state.get_data()
    results = data.get("edit_start_geo_results", [])

    if idx >= len(results):
        await callback.answer("Неверный выбор.", show_alert=True)
        return

    r = results[idx]
    await callback.answer()
    await state.update_data(
        edit_new_start_address=r["full_address"],
        edit_new_start_lat=r["lat"],
        edit_new_start_lon=r["lon"],
    )
    await _maybe_ask_about_stop(callback.message, state)


async def _maybe_ask_about_stop(message: Message, state: FSMContext):
    """Если у рейса задана остановка старта — спросить, оставить или сбросить."""
    data = await state.get_data()
    trip = storage.get_trip(message.chat.id, data["edit_trip_id"])

    if trip and trip.preferred_start_stop:
        await state.set_state(EditTripStates.waiting_start_stop_keep)
        keyboard = _append_cancel([
            [InlineKeyboardButton(text=f"Оставить «{trip.preferred_start_stop}»", callback_data="keepstop:keep")],
            [InlineKeyboardButton(text="Задать новую", callback_data="keepstop:new")],
            [InlineKeyboardButton(text="Сбросить", callback_data="keepstop:reset")],
        ])
        await message.answer(
            f"У рейса задана остановка старта: «{trip.preferred_start_stop}»\n"
            f"Адрес старта изменился — что сделать с остановкой?",
            reply_markup=keyboard,
        )
    else:
        await _finish_edit_start(message, state, reset_stop=False)


@router.callback_query(F.data.startswith("keepstop:"), EditTripStates.waiting_start_stop_keep)
async def cb_keep_stop(callback: CallbackQuery, state: FSMContext):
    choice = callback.data.split(":", 1)[1]
    await callback.answer()

    if choice == "new":
        await state.set_state(EditTripStates.waiting_new_stop_name)
        await callback.message.answer("Введите название новой остановки старта:")
        return

    await _finish_edit_start(callback.message, state, reset_stop=(choice == "reset"))


@router.message(EditTripStates.waiting_new_stop_name)
async def edit_new_stop_name(message: Message, state: FSMContext):
    new_stop = message.text.strip()
    await state.update_data(edit_new_stop_name=new_stop)
    await _finish_edit_start(message, state, reset_stop=False, new_stop=new_stop)


async def _finish_edit_start(
    message: Message,
    state: FSMContext,
    reset_stop: bool,
    new_stop: str | None = None,
):
    """Сохранить новый адрес старта."""
    data = await state.get_data()
    trip = storage.get_trip(message.chat.id, data["edit_trip_id"])
    if not trip:
        await message.answer("Рейс не найден.")
        await state.clear()
        return

    old_address = trip.start_address
    trip.start_address = data["edit_new_start_address"]
    trip.start_lat = data["edit_new_start_lat"]
    trip.start_lon = data["edit_new_start_lon"]

    if new_stop:
        trip.preferred_start_stop = new_stop
    elif reset_stop:
        trip.preferred_start_stop = None

    storage.update_trip(message.from_user.id, trip)
    await state.clear()

    stop_note = ""
    if new_stop:
        stop_note = f"\n🚏 Остановка старта: «{new_stop}»"
    elif reset_stop:
        stop_note = "\n🚏 Остановка старта сброшена."

    await message.answer(
        f"Адрес старта изменён:\n"
        f"📍 {old_address} → {trip.start_address}{stop_note}"
    )


# ── Изменение адреса финиша ───────────────────────────────────

@router.message(EditTripStates.waiting_end_address)
async def edit_trip_end_address(message: Message, state: FSMContext):
    query = message.text.strip()
    try:
        results = await geocode(query, city_name=DEFAULT_CITY)
    except Exception as e:
        logger.exception("Ошибка геокодинга")
        await message.answer(f"Ошибка поиска адреса: {e}\nПопробуйте ещё раз:")
        return

    if not results:
        await message.answer("Адрес не найден. Попробуйте ввести более точный адрес:")
        return

    await state.update_data(
        edit_end_geo_results=[
            {"name": r.name, "full_address": r.full_address, "lat": r.lat, "lon": r.lon}
            for r in results
        ]
    )

    if len(results) == 1:
        r = results[0]
        await _finish_edit_end(message, state, r.full_address, r.lat, r.lon)
    else:
        await state.set_state(EditTripStates.waiting_end_select)
        keyboard = _geo_keyboard(results, "editgeo_end")
        await message.answer("Выберите адрес финиша:", reply_markup=keyboard)


@router.callback_query(F.data.startswith("editgeo_end:"))
async def cb_edit_select_end(callback: CallbackQuery, state: FSMContext):
    idx = int(callback.data.split(":", 1)[1])
    data = await state.get_data()
    results = data.get("edit_end_geo_results", [])

    if idx >= len(results):
        await callback.answer("Неверный выбор.", show_alert=True)
        return

    r = results[idx]
    await callback.answer()
    await _finish_edit_end(callback.message, state, r["full_address"], r["lat"], r["lon"])


async def _finish_edit_end(message: Message, state: FSMContext, address: str, lat: float, lon: float):
    """Сохранить новый адрес финиша."""
    data = await state.get_data()
    trip = storage.get_trip(message.chat.id, data["edit_trip_id"])
    if not trip:
        await message.answer("Рейс не найден.")
        await state.clear()
        return

    old_address = trip.end_address
    trip.end_address = address
    trip.end_lat = lat
    trip.end_lon = lon
    storage.update_trip(message.from_user.id, trip)
    await state.clear()
    await message.answer(
        f"Адрес финиша изменён:\n"
        f"🏁 {old_address} → {trip.end_address}"
    )


# ── /go — пора на выход (разовое уведомление) ─────────────────

@router.message(Command("go"))
async def cmd_go(message: Message, state: FSMContext):
    trips = storage.get_trips(message.from_user.id)
    if not trips:
        await message.answer("Нет сохранённых рейсов.")
        return

    keyboard = _append_cancel([
        [InlineKeyboardButton(
            text=f"{'🟢' if t.go_notify_minutes else '⚪'} {t.name}",
            callback_data=f"go:{t.id}",
        )]
        for t in trips
    ])
    await message.answer(
        "🚶 Пора на выход — разовое уведомление о ближайшем автобусе.\n"
        "Выберите рейс:",
        reply_markup=keyboard,
    )


@router.callback_query(F.data.startswith("go:"))
async def cb_go(callback: CallbackQuery, state: FSMContext):
    trip_id = callback.data.split(":", 1)[1]
    trip = storage.get_trip(callback.from_user.id, trip_id)
    if not trip:
        await callback.answer("Рейс не найден.", show_alert=True)
        return

    await callback.answer()

    # Если уже активно — предлагаем отключить
    if trip.go_notify_minutes:
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="Изменить", callback_data=f"goset:{trip_id}")],
                [InlineKeyboardButton(text="Отключить", callback_data=f"gooff:{trip_id}")],
            ]
        )
        exit_info = f"+{trip.exit_minutes} на выход" if trip.exit_minutes else ""
        await callback.message.answer(
            f"🟢 Гоу-уведомление для «{trip.name}» уже активно "
            f"(за {trip.go_notify_minutes}{exit_info} мин, окно {trip.go_notify_from}–{trip.go_notify_to}).\n"
            f"Что сделать?",
            reply_markup=keyboard,
        )
        return

    await state.update_data(go_trip_id=trip_id)
    await state.set_state(GoNotifyStates.waiting_minutes)
    exit_note = f"\n💡 Время на выход ({trip.exit_minutes} мин) будет добавлено автоматически." if trip.exit_minutes else ""
    await callback.message.answer(
        f"Рейс: «{trip.name}»\n\n"
        f"За сколько минут до ближайшего автобуса вас уведомить?\n"
        f"Введите число (например: 10):{exit_note}"
    )


@router.callback_query(F.data.startswith("goset:"))
async def cb_go_set(callback: CallbackQuery, state: FSMContext):
    trip_id = callback.data.split(":", 1)[1]
    trip = storage.get_trip(callback.from_user.id, trip_id)
    await callback.answer()
    await state.update_data(go_trip_id=trip_id)
    await state.set_state(GoNotifyStates.waiting_minutes)
    exit_note = f"\n💡 Время на выход ({trip.exit_minutes} мин) будет добавлено автоматически." if trip and trip.exit_minutes else ""
    await callback.message.answer(
        f"За сколько минут до ближайшего автобуса вас уведомить?\n"
        f"Введите число (например: 10):{exit_note}"
    )


@router.callback_query(F.data.startswith("gooff:"))
async def cb_go_off(callback: CallbackQuery):
    trip_id = callback.data.split(":", 1)[1]
    trip = storage.get_trip(callback.from_user.id, trip_id)
    if not trip:
        await callback.answer("Рейс не найден.", show_alert=True)
        return

    trip.go_notify_minutes = None
    trip.go_notify_from = None
    trip.go_notify_to = None
    trip.go_notify_date = None
    storage.update_trip(callback.from_user.id, trip)
    await callback.answer()
    await callback.message.edit_text(f"⚪ Гоу-уведомление для «{trip.name}» отключено.")


@router.message(GoNotifyStates.waiting_minutes)
async def go_set_minutes(message: Message, state: FSMContext):
    data = await state.get_data()
    trip_id = data.get("go_trip_id")
    trip = storage.get_trip(message.from_user.id, trip_id)
    if not trip:
        await message.answer("Рейс не найден.")
        await state.clear()
        return

    text = message.text.strip()
    try:
        minutes = int(text)
    except ValueError:
        await message.answer("Введите число минут (например: 10):")
        return

    if minutes <= 0:
        await message.answer("Число должно быть положительным. Введите ещё раз:")
        return

    from datetime import date as _date
    from .models import _minutes_to_hhmm, _now_minutes
    now_min = _now_minutes()
    go_from = _minutes_to_hhmm(now_min)
    go_to = _minutes_to_hhmm(now_min + 20)

    trip.go_notify_minutes = minutes
    trip.go_notify_from = go_from
    trip.go_notify_to = go_to
    trip.go_notify_date = _date.today().isoformat()
    storage.update_trip(message.from_user.id, trip)
    await state.clear()
    exit_hint = f" + {trip.exit_minutes} мин на выход = {minutes + trip.exit_minutes} мин" if trip.exit_minutes else ""
    await message.answer(
        f"🟢 Гоу-уведомление для «{trip.name}» активировано!\n"
        f"Уведомлю за {minutes} мин до ближайшего автобуса{exit_hint}.\n"
        f"Окно: {go_from}–{go_to} (после этого отключится автоматически)."
    )


# ── /delete — удалить рейс ─────────────────────────────────────

@router.message(Command("delete"))
async def cmd_delete(message: Message):
    trips = storage.get_trips(message.from_user.id)
    if not trips:
        await message.answer("Нет сохранённых рейсов.")
        return

    keyboard = _append_cancel([
        [InlineKeyboardButton(text=f"❌ {t.name}", callback_data=f"del:{t.id}")]
        for t in trips
    ])
    await message.answer("Выберите рейс для удаления:", reply_markup=keyboard)


@router.callback_query(F.data.startswith("del:"))
async def cb_delete(callback: CallbackQuery):
    trip_id = callback.data.split(":", 1)[1]
    deleted = storage.delete_trip(callback.from_user.id, trip_id)
    if deleted:
        await callback.answer("Рейс удалён.")
        await callback.message.edit_text("Рейс удалён.")
    else:
        await callback.answer("Рейс не найден.", show_alert=True)


# ── /ksearch — поиск маршрутов через kudikina.ru ──────────────

@router.message(Command("ksearch"))
async def cmd_ksearch(message: Message, state: FSMContext):
    # Предлагаем выбрать город или использовать Омск по умолчанию
    popular = ["omsk", "msk", "spb", "tatar", "sverd", "krasd", "samar", "nizhe"]
    buttons = []
    for slug in popular:
        name = CITY_SLUGS.get(slug, slug)
        buttons.append([InlineKeyboardButton(text=name, callback_data=f"kcity:{slug}")])

    keyboard = _append_cancel(buttons)
    await message.answer(
        "🔍 Поиск маршрутов (kudikina.ru)\n\n"
        "Выберите город:",
        reply_markup=keyboard,
    )
    await state.set_state(KSearchStates.waiting_city)


@router.callback_query(KSearchStates.waiting_city, F.data.startswith("kcity:"))
async def cb_ksearch_city(callback: CallbackQuery, state: FSMContext):
    slug = callback.data.split(":", 1)[1]
    city_name = CITY_SLUGS.get(slug, slug)
    await state.update_data(city_slug=slug, city_name=city_name)
    await callback.answer()
    await callback.message.edit_text(
        f"Город: {city_name}\n\n"
        "Введите название остановки отправления:"
    )
    await state.set_state(KSearchStates.waiting_from_stop)


@router.message(KSearchStates.waiting_from_stop)
async def ksearch_from_stop(message: Message, state: FSMContext):
    from_stop = message.text.strip()
    if not from_stop:
        await message.answer("Введите название остановки.")
        return

    await state.update_data(from_stop=from_stop)
    await message.answer(
        f"Откуда: {from_stop}\n\n"
        "Введите название остановки назначения:"
    )
    await state.set_state(KSearchStates.waiting_to_stop)


@router.message(KSearchStates.waiting_to_stop)
async def ksearch_to_stop(message: Message, state: FSMContext):
    to_stop = message.text.strip()
    if not to_stop:
        await message.answer("Введите название остановки.")
        return

    data = await state.get_data()
    city_slug = data["city_slug"]
    city_name = data["city_name"]
    from_stop = data["from_stop"]
    await state.clear()

    await message.answer(
        f"🔎 Ищу маршруты: {from_stop} → {to_stop} ({city_name})..."
    )

    try:
        routes = await kudikina_search(city_slug, from_stop, to_stop)
    except Exception as e:
        logger.error("Ошибка kudikina search: %s", e)
        await message.answer("❌ Ошибка при поиске маршрутов. Попробуйте позже.")
        return

    if not routes:
        await message.answer(
            f"Маршруты от «{from_stop}» до «{to_stop}» не найдены.\n\n"
            "Проверьте правильность названий остановок."
        )
        return

    # Формируем ответ
    header = f"🗺 Маршруты: {from_stop} → {to_stop}\n\n"
    parts = []
    for i, route in enumerate(routes):
        if i >= 8:  # ограничение — не более 8 маршрутов
            parts.append(f"\n... и ещё {len(routes) - 8} маршрут(ов)")
            break
        parts.append(route.format_summary())

    text = header + "\n\n".join(parts)
    if len(text) > 4000:
        text = text[:4000] + "\n\n... (сокращено)"

    await message.answer(text)
