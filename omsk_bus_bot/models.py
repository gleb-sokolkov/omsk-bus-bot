"""Модели данных."""

import re
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Optional


def _now_minutes() -> int:
    """Текущее время в минутах от начала дня."""
    now = datetime.now()
    return now.hour * 60 + now.minute


@dataclass
class Trip:
    """Сохранённый рейс пользователя."""
    id: str
    name: str
    start_lat: float
    start_lon: float
    end_lat: float
    end_lon: float
    start_address: str
    end_address: str
    notify_minutes: Optional[int] = None  # За сколько минут до прибытия автобуса уведомлять (None = выкл)
    notify_from: str = "07:00"  # Начало окна уведомлений "HH:MM"
    notify_to: str = "08:00"   # Конец окна уведомлений "HH:MM"
    go_notify_minutes: Optional[int] = None  # Гоу-уведомление: за сколько минут (None = выкл)
    go_notify_from: Optional[str] = None     # Гоу-уведомление: начало окна "HH:MM"
    go_notify_to: Optional[str] = None       # Гоу-уведомление: конец окна "HH:MM"
    go_notify_date: Optional[str] = None     # Гоу-уведомление: дата создания "YYYY-MM-DD"
    exit_minutes: int = 0                     # Время на выход из дома (мин), учитывается в departure
    kudikina_start_stop: Optional[str] = None  # Остановка старта для kudikina (ручной ввод)
    kudikina_end_stop: Optional[str] = None    # Остановка финиша для kudikina (ручной ввод)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Trip":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


def _minutes_to_hhmm(minutes: int) -> str:
    """Преобразовать минуты от начала дня в строку H:MM."""
    minutes = minutes % (24 * 60)
    return f"{minutes // 60}:{minutes % 60:02d}"


def _hhmm_to_minutes(hhmm: str) -> Optional[int]:
    """Преобразовать строку HH:MM или H:MM в минуты от начала дня."""
    match = re.match(r"(\d{1,2}):(\d{2})", hhmm)
    if not match:
        return None
    return int(match.group(1)) * 60 + int(match.group(2))


def _parse_walk_minutes(comment: str) -> int:
    """Извлечь длительность пешего участка из комментария вроде 'пешком 390 м'.

    Оценка: скорость пешехода ~70 м/мин. Округление вверх.
    """
    dist_match = re.search(r"(\d+)\s*м", comment)
    if dist_match:
        meters = int(dist_match.group(1))
        return max(1, (meters + 69) // 70)
    return 0


@dataclass
class RouteInfo:
    """Краткая информация о маршруте из ответа 2GIS."""
    route_id: str
    total_duration: int  # секунды
    total_distance: int  # метры
    transfer_count: int
    crossing_count: int
    pedestrian: bool
    total_walkway_distance: str
    movements: list = field(default_factory=list)
    schedules: list = field(default_factory=list)  # Расписание транспорта от API
    start_stop_name: Optional[str] = None  # Название первой остановки (passage)

    @property
    def duration_minutes(self) -> int:
        return self.total_duration // 60

    # ── Извлечение данных о транспорте из movements ─────────────

    def extract_passage_info(self) -> list[dict]:
        """Извлечь из movements данные о каждом транспортном сегменте.

        Возвращает список словарей:
        [
            {
                "start_stop": "По требованию",
                "end_stop": "Студенческая",
                "bus_numbers": ["24"],
                "all_stops": ["По требованию", "3-я Енисейская", ..., "Студенческая"],
            },
            ...  # при пересадках — несколько элементов
        ]

        end_stop определяется из waypoint.name следующего movement
        (остановка высадки), а не из platforms.names (промежуточные остановки).
        """
        segments = []
        movements = self.movements
        for i, m in enumerate(movements):
            if m.get("type") != "passage":
                continue

            waypoint = m.get("waypoint", {})
            start_stop = waypoint.get("name", "")

            # Номера маршрутов
            bus_numbers = []
            for r in m.get("routes", []):
                for n in r.get("names", []):
                    bus_numbers.append(n)

            # Список промежуточных остановок
            platforms = m.get("platforms", {})
            all_stops = platforms.get("names", [])

            # Конечная остановка — waypoint.name следующего movement
            # (crossing, walkway/pedestrian или walkway/finish)
            end_stop = ""
            if i + 1 < len(movements):
                next_wp = movements[i + 1].get("waypoint", {})
                end_stop = next_wp.get("name", "")

            # Fallback: если следующий movement не дал имя, берём последнюю из platforms
            if not end_stop and all_stops:
                end_stop = all_stops[-1]

            # Добавляем end_stop в all_stops, если его там нет
            if end_stop and (not all_stops or all_stops[-1] != end_stop):
                all_stops = all_stops + [end_stop]

            segments.append({
                "start_stop": start_stop,
                "end_stop": end_stop,
                "bus_numbers": bus_numbers,
                "all_stops": all_stops,
            })

        return segments

    # ── Разбор структуры movements ─────────────────────────────

    def _walk_before_stop_minutes(self) -> int:
        """Длительность пешей части ДО первой остановки (от старта до passage)."""
        total = 0
        for m in self.movements:
            if m.get("type") == "passage":
                break
            comment = m.get("waypoint", {}).get("comment", "")
            total += _parse_walk_minutes(comment)
        return total

    def _walk_after_stop_minutes(self) -> int:
        """Длительность пешей части ПОСЛЕ последнего passage (до финиша)."""
        total = 0
        after_last_passage = False
        for m in self.movements:
            if m.get("type") == "passage":
                after_last_passage = True
                total = 0  # сбрасываем — считаем только после последнего passage
            elif after_last_passage:
                subtype = m.get("waypoint", {}).get("subtype", "")
                if subtype in ("pedestrian", "finish"):
                    comment = m.get("waypoint", {}).get("comment", "")
                    total += _parse_walk_minutes(comment)
        return total

    def _ride_distance_meters(self) -> int:
        """Дистанция транспортных участков (passage) в метрах."""
        total = 0
        for m in self.movements:
            if m.get("type") == "passage":
                total += m.get("distance", 0)
        return total

    def _ride_duration_minutes(self) -> int:
        """Длительность поездки на транспорте (без ожидания и пеших участков).

        Стратегия:
        1. Если у passage-movements есть moving_duration и он «разумный»
           (не больше чем distance / 5 м/с * 2), используем его.
        2. Иначе оцениваем из distance: городской автобус ~20 км/ч = ~3 мин/км.
        """
        ride_sec = 0
        ride_dist = 0
        has_moving = False

        for m in self.movements:
            if m.get("type") == "passage":
                dist = m.get("distance", 0)
                ride_dist += dist
                md = m.get("moving_duration")
                if md is not None and md > 0:
                    # Проверяем разумность: не более 6 мин/км (~10 км/ч — медленный автобус)
                    max_reasonable = max(dist * 6 / 1000 * 60, 120)  # секунды, минимум 2 мин
                    if md <= max_reasonable:
                        ride_sec += md
                        has_moving = True
                    # Если moving_duration неразумно большой — пропускаем, пойдём в оценку

        if has_moving and ride_sec > 0:
            return max(1, ride_sec // 60)

        # Оценка: ~20 км/ч = 3 мин/км
        if ride_dist > 0:
            return max(1, (ride_dist * 3 + 999) // 1000)

        # Совсем fallback из total_distance
        if self.total_distance > 0:
            return max(1, (self.total_distance * 3 + 999) // 1000)

        return 5  # абсолютный fallback

    def _first_schedule_minutes(self) -> Optional[int]:
        """Первое будущее время прибытия транспорта из schedules (минуты от начала дня)."""
        upcoming = self.all_schedule_minutes()
        return upcoming[0] if upcoming else None

    def all_schedule_minutes(self) -> list[int]:
        """Все будущие времена прибытия транспорта из schedules (минуты от начала дня).

        Возвращает только рейсы, которые ещё не прошли (>= текущего времени).
        """
        now_min = _now_minutes()
        result = []
        for s in self.schedules:
            precise = s.get("precise_time", "")
            if precise:
                m = _hhmm_to_minutes(precise)
                if m is not None and m >= now_min:
                    result.append(m)
            else:
                st = s.get("start_time")
                if st is not None and st > 0:
                    m = st // 60
                    if m >= now_min:
                        result.append(m)
        return sorted(result)

    def _parse_arrival_time(self) -> Optional[str]:
        """Извлечь время прибытия из комментария финишного movement."""
        for m in reversed(self.movements):
            waypoint = m.get("waypoint", {})
            if waypoint.get("subtype") == "finish":
                comment = waypoint.get("comment", "")
                match = re.search(r"(\d{1,2}:\d{2})", comment)
                if match:
                    return match.group(1)
        return None

    # ── Расчёт времён на основе расписания ─────────────────────

    def _calc_times_from_schedule(self, exit_minutes: int = 0, target_boarding_min: Optional[int] = None) -> Optional[dict]:
        """Рассчитать реальные времена на основе расписания.

        Логика:
        - boarding = целевое время из расписания (когда автобус на остановке)
        - departure = boarding - walk_before - exit_minutes (когда выйти из дома)
        - arrival = boarding + ride_duration + walk_after (когда дойдёшь до финиша)

        exit_minutes — фиксированное время на выход из дома (спуск с этажа и т.д.)
        target_boarding_min — конкретное время посадки (если None, берётся ближайшее).
        ride_duration вычисляется из total_duration без пеших частей и ожидания.
        """
        boarding_min = target_boarding_min if target_boarding_min is not None else self._first_schedule_minutes()
        if boarding_min is None:
            return None

        walk_before = self._walk_before_stop_minutes()
        walk_after = self._walk_after_stop_minutes()
        ride = self._ride_duration_minutes()

        departure_min = boarding_min - walk_before - exit_minutes
        arrival_min = boarding_min + ride + walk_after
        total_min = exit_minutes + walk_before + ride + walk_after

        return {
            "departure": _minutes_to_hhmm(departure_min),
            "boarding": _minutes_to_hhmm(boarding_min),
            "arrival": _minutes_to_hhmm(arrival_min),
            "total_min": total_min,
        }

    # ── Форматирование расписания ──────────────────────────────

    def _format_schedule_list(self, max_items: int = 0, from_min: Optional[int] = None) -> Optional[str]:
        """Список будущих времён прибытия транспорта из schedules.

        Args:
            max_items: максимальное количество записей (0 = без ограничения).
            from_min: показывать только рейсы начиная с этого времени (минуты от начала дня).
                      Если None — от текущего момента.
        """
        if not self.schedules:
            return None

        start_min = from_min if from_min is not None else _now_minutes()
        parts = []
        for s in self.schedules:
            if max_items > 0 and len(parts) >= max_items:
                break
            stype = s.get("type")
            if stype == "precise":
                precise = s.get("precise_time", "")
                if precise:
                    m = _hhmm_to_minutes(precise)
                    if m is not None and m >= start_min:
                        parts.append(precise)
                else:
                    st = s.get("start_time", 0)
                    if st // 60 >= start_min:
                        parts.append(f"{st // 3600}:{(st % 3600) // 60:02d}")
            elif stype == "periodic":
                st = s.get("start_time", 0)
                period = s.get("period")
                time_str = s.get("precise_time") or f"{st // 3600}:{(st % 3600) // 60:02d}"
                m = _hhmm_to_minutes(time_str) if isinstance(time_str, str) and ":" in time_str else (st // 60)
                if m is not None and m >= start_min:
                    if period:
                        parts.append(f"{time_str} (каждые {period} мин)")
                    else:
                        parts.append(time_str)

        return ", ".join(parts) if parts else None

    # ── Главный метод форматирования ───────────────────────────

    def format_summary(self, max_schedule_items: int = 0, exit_minutes: int = 0, target_boarding_min: Optional[int] = None) -> str:
        """Форматирование краткой сводки маршрута.

        Args:
            max_schedule_items: максимум записей расписания (0 = без ограничения).
            exit_minutes: время на выход из дома (мин).
            target_boarding_min: конкретное время посадки для расчёта (если None — ближайшее).
        """
        lines = []

        # Пытаемся рассчитать реальные времена от расписания
        sched_times = self._calc_times_from_schedule(exit_minutes=exit_minutes, target_boarding_min=target_boarding_min)
        api_arrival = self._parse_arrival_time()

        if sched_times:
            dep = sched_times["departure"]
            board = sched_times["boarding"]
            arr = sched_times["arrival"]
            total = sched_times["total_min"]
            lines.append(f"🕐 Выход {dep} → посадка {board} → прибытие ~{arr} ({total} мин)")
        elif api_arrival:
            lines.append(f"🕐 Прибытие в {api_arrival} ({self.duration_minutes} мин)")
        else:
            lines.append(f"⏱ {self.duration_minutes} мин")

        # Список расписания (от целевого времени посадки, если задано)
        schedule_list = self._format_schedule_list(max_items=max_schedule_items or 0, from_min=target_boarding_min)
        if schedule_list:
            lines.append(f"🚏 Расписание: {schedule_list}")

        lines.append(f"📏 {self.total_distance} м | 🚶 {self.total_walkway_distance}")
        if self.transfer_count > 0:
            lines.append(f"🔄 Пересадок: {self.transfer_count}")

        # Описание сегментов маршрута
        for m in self.movements:
            m_type = m.get("type")
            waypoint = m.get("waypoint", {})
            wp_name = waypoint.get("name", "")
            wp_comment = waypoint.get("comment", "")
            subtype = waypoint.get("subtype", "")

            if m_type == "passage":
                routes = m.get("routes", [])
                route_names = []
                for r in (routes or []):
                    names = r.get("names", [])
                    subtype_name = r.get("subtype_name", r.get("subtype", ""))
                    for n in (names or []):
                        route_names.append(f"{subtype_name} {n}")
                routes_str = ", ".join(route_names) if route_names else "транспорт"
                lines.append(f"  🚌 {wp_name} → {routes_str}")

                # Промежуточные остановки
                platforms = m.get("platforms")
                if platforms and platforms.get("names"):
                    stops = platforms["names"]
                    lines.append(f"     Остановки: {' → '.join(stops)}")
            elif m_type == "walkway" and subtype == "start":
                lines.append(f"  📍 Старт: {wp_name}")
                if wp_comment:
                    lines.append(f"     {wp_comment}")
            elif m_type == "walkway" and subtype == "finish":
                lines.append(f"  🏁 Финиш: {wp_name}")
            elif m_type == "walkway" and subtype == "pedestrian":
                if wp_comment:
                    lines.append(f"  🚶 {wp_name}: {wp_comment}")
            elif m_type == "crossing":
                lines.append(f"  🔄 Пересадка: {wp_name} ({wp_comment})")

        return "\n".join(lines)
