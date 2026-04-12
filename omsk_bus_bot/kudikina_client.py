"""Клиент для kudikina.ru — расписания автобусов."""

import re
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

KUDIKINA_BASE = "https://kudikina.ru"

# Город по умолчанию (slug для URL)
DEFAULT_CITY_SLUG = "omsk"

# Популярные города: slug → отображаемое название
CITY_SLUGS = {
    "omsk": "Омск",
    "msk": "Москва",
    "spb": "Санкт-Петербург",
    "arhan": "Архангельская обл.",
    "bashk": "Башкортостан",
    "belgo": "Белгородская обл.",
    "volga": "Волгоградская обл.",
    "kalin": "Калининградская обл.",
    "kemer": "Кемеровская обл.",
    "krasd": "Краснодарский край",
    "krask": "Красноярский край",
    "krim": "Крым",
    "lenin": "Ленинградская обл.",
    "mos": "Московская обл.",
    "nizhe": "Нижегородская обл.",
    "perms": "Пермский край",
    "rosto": "Ростовская обл.",
    "samar": "Самарская обл.",
    "sverd": "Свердловская обл.",
    "stavr": "Ставропольский край",
    "tatar": "Татарстан",
    "tulsk": "Тульская обл.",
    "tumen": "Тюменская обл.",
}


@dataclass
class TransferLeg:
    """Часть маршрута с пересадкой."""
    transport_type: str   # "Маршрутка"
    number: str           # "275"
    direction: str        # "Ул. Дергачева — МСЧ-9"
    link: str = ""


@dataclass
class ScheduleBlock:
    """Блок расписания — отправления с конкретной остановки."""
    stop_name: str                            # "Проспект Комарова" или "Ул. Дмитриева"
    times: list[str] = field(default_factory=list)
    special_marks: dict[str, str] = field(default_factory=dict)


@dataclass
class KudikinaRoute:
    """Маршрут из результатов поиска kudikina.ru."""
    transport_type: str       # "Автобус", "Маршрутка", "Троллейбус", "Трамвай"
    number: str               # "24", "500"
    direction: str            # "Железнодорожный вокзал — Пос. Солнечный"
    stop_count: int           # количество остановок
    stops: list[str] = field(default_factory=list)
    schedules: list[ScheduleBlock] = field(default_factory=list)  # один или несколько блоков расписания
    transfer: Optional[TransferLeg] = None   # пересадочный маршрут
    link: str = ""            # ссылка на маршрут

    @property
    def is_transfer(self) -> bool:
        return self.transfer is not None

    @property
    def times(self) -> list[str]:
        """Времена первого блока расписания (обратная совместимость)."""
        return self.schedules[0].times if self.schedules else []

    @property
    def special_marks(self) -> dict[str, str]:
        """Обозначения первого блока расписания."""
        return self.schedules[0].special_marks if self.schedules else {}

    def upcoming_times(self, schedule_idx: int = 0, count: int = 5, from_minutes: Optional[int] = None) -> list[str]:
        """Ближайшие будущие рейсы из указанного блока расписания.

        Args:
            from_minutes: показывать рейсы начиная с этого времени (минуты от начала дня).
                          Если None — от текущего момента.
        """
        if schedule_idx >= len(self.schedules):
            return []
        start_min = from_minutes if from_minutes is not None else (datetime.now().hour * 60 + datetime.now().minute)
        upcoming = []
        for t in self.schedules[schedule_idx].times:
            clean = re.match(r"(\d{1,2}):(\d{2})", t)
            if clean:
                m = int(clean.group(1)) * 60 + int(clean.group(2))
                if m >= start_min:
                    upcoming.append(t)
                    if len(upcoming) >= count:
                        break
        return upcoming

    def all_schedule_minutes(self, schedule_idx: int = 0) -> list[int]:
        """Все времена отправления в минутах от начала дня (первый блок расписания).

        Возвращает ВСЕ времена за день (без фильтрации прошедших) — чистые
        минуты, пригодные для подстановки в RouteInfo.schedules.
        """
        if schedule_idx >= len(self.schedules):
            return []
        result = []
        for t in self.schedules[schedule_idx].times:
            clean = re.match(r"(\d{1,2}):(\d{2})", t)
            if clean:
                result.append(int(clean.group(1)) * 60 + int(clean.group(2)))
        return result

    def to_2gis_schedules(self, schedule_idx: int = 0) -> list[dict]:
        """Конвертировать расписание kudikina в формат 2GIS schedules.

        Формат: [{"type": "precise", "precise_time": "6:43", "start_time": 24180}, ...]
        Совместим с RouteInfo.all_schedule_minutes() и _format_schedule_list().
        """
        result = []
        for t in self.schedules[schedule_idx].times if schedule_idx < len(self.schedules) else []:
            clean = re.match(r"(\d{1,2}):(\d{2})", t)
            if clean:
                h, m = int(clean.group(1)), int(clean.group(2))
                result.append({
                    "type": "precise",
                    "precise_time": f"{h}:{m:02d}",
                    "start_time": h * 3600 + m * 60,
                })
        return result

    def _format_schedule_block(self, idx: int, show_upcoming: int = 5) -> list[str]:
        """Форматировать один блок расписания."""
        if idx >= len(self.schedules):
            return []
        sched = self.schedules[idx]
        lines = []
        upcoming = self.upcoming_times(idx, show_upcoming)
        label = f"от {sched.stop_name}" if sched.stop_name else ""
        if upcoming:
            lines.append(f"   ⏱ Ближайшие {label}: {', '.join(upcoming)}")
        elif sched.times:
            last_few = sched.times[-3:]
            lines.append(f"   ⏱ Рейсов {label} больше нет (последние: {', '.join(last_few)})")
        else:
            lines.append(f"   ⏱ Расписание {label} недоступно")

        if sched.special_marks:
            marks_str = "; ".join(f"{k} — {v}" for k, v in sched.special_marks.items())
            lines.append(f"   📝 {marks_str}")
        return lines

    def format_summary(self, show_upcoming: int = 5) -> str:
        """Форматирование для отправки в Telegram."""
        lines = []
        emoji = {"Автобус": "🚌", "Маршрутка": "🚐", "Троллейбус": "🚎", "Трамвай": "🚋"}
        icon = emoji.get(self.transport_type, "🚍")

        header = f"{icon} {self.transport_type} {self.number}"
        if self.transfer:
            t = self.transfer
            t_icon = emoji.get(t.transport_type, "🚍")
            header += f"\n   🔄 пересадка → {t_icon} {t.transport_type} {t.number}"
        lines.append(header)

        lines.append(f"   {self.direction}")
        if self.transfer:
            lines.append(f"   ↳ {self.transfer.direction}")

        lines.append(f"   Остановок: {self.stop_count}")

        if self.stops:
            route_str = " → ".join(self.stops)
            if len(route_str) > 200:
                route_str = route_str[:200] + "…"
            lines.append(f"   🚏 {route_str}")

        # Расписания
        if not self.schedules:
            lines.append("   ⏱ Расписание недоступно")
        else:
            for i in range(len(self.schedules)):
                lines.extend(self._format_schedule_block(i, show_upcoming))

        return "\n".join(lines)


def _parse_search_html(html: str, from_stop: str) -> list[KudikinaRoute]:
    """Разобрать HTML страницы результатов поиска kudikina.ru."""
    routes: list[KudikinaRoute] = []

    container_match = re.search(
        r'<div[^>]*class="[^"]*search-buses[^"]*"[^>]*>(.*)',
        html, re.DOTALL,
    )
    if not container_match:
        return routes

    container_html = container_match.group(1)

    # Разбиваем по <div class="row">
    row_blocks = re.split(r'<div[^>]*class="row"[^>]*>', container_html)

    for block in row_blocks[1:]:  # skip first (before first row)
        route = _parse_route_block(block, from_stop)
        if route:
            routes.append(route)

    return routes


def _parse_route_block(block: str, from_stop: str) -> Optional[KudikinaRoute]:
    """Разобрать один блок маршрута."""
    # Первый маршрут: тип и номер из <a>
    bus_match = re.search(
        r'<a[^>]*href="([^"]*)"[^>]*title="([^"]*)"[^>]*>\s*'
        r'((?:Автобус|Маршрутка|Троллейбус|Трамвай)\s+\S+)',
        block,
    )
    if not bus_match:
        return None

    link = bus_match.group(1)
    type_and_number = bus_match.group(3).strip()

    # Разделяем тип и номер
    parts = type_and_number.split(None, 1)
    transport_type = parts[0] if parts else "Автобус"
    number = parts[1] if len(parts) > 1 else ""

    # Направление из <span> после первой ссылки
    direction_match = re.search(r'</a>\s*<span>([^<]+)</span>', block)
    direction = direction_match.group(1).strip() if direction_match else ""

    # Пересадка: ищем second-bus блок
    transfer = None
    second_bus_match = re.search(
        r'class="second-bus"[^>]*>.*?'
        r'<a[^>]*href="([^"]*)"[^>]*title="([^"]*)"[^>]*>\s*'
        r'((?:Автобус|Маршрутка|Троллейбус|Трамвай)\s+\S+).*?'
        r'<span>([^<]+)</span>',
        block, re.DOTALL,
    )
    if second_bus_match:
        t_link = second_bus_match.group(1)
        t_type_num = second_bus_match.group(3).strip()
        t_dir = second_bus_match.group(4).strip()
        t_parts = t_type_num.split(None, 1)
        transfer = TransferLeg(
            transport_type=t_parts[0] if t_parts else "Автобус",
            number=t_parts[1] if len(t_parts) > 1 else "",
            direction=t_dir,
            link=KUDIKINA_BASE + t_link if t_link.startswith("/") else t_link,
        )

    # Количество остановок
    stop_count_match = re.search(r'(\d+)\s*остановок', block)
    if not stop_count_match:
        stop_count_match = re.search(r'(\d+)\s*остановку', block)
    stop_count = int(stop_count_match.group(1)) if stop_count_match else 0

    # Список остановок
    stops = _parse_stops(block)

    # Блоки расписания (может быть несколько)
    schedules = _parse_schedules(block)

    return KudikinaRoute(
        transport_type=transport_type,
        number=number,
        direction=direction,
        stop_count=stop_count,
        stops=stops,
        schedules=schedules,
        transfer=transfer,
        link=KUDIKINA_BASE + link if link.startswith("/") else link,
    )


def _parse_stops(block: str) -> list[str]:
    """Извлечь список остановок из блока."""
    stations_match = re.search(
        r'search-bus-stations[^"]*"[^>]*>(.*?)(?:</div>\s*<div|</div>\s*$)',
        block, re.DOTALL,
    )
    if not stations_match:
        return []

    stations_html = stations_match.group(1)
    li_items = re.findall(r'<li>([^<]+)</li>', stations_html)
    return [item.strip() for item in li_items if item.strip()]


def _parse_schedules(block: str) -> list[ScheduleBlock]:
    """Извлечь все блоки расписания из маршрута.

    Маршрут может содержать несколько блоков: основной + расписание пересадочного.
    Каждый блок содержит:
    - <span>время отъезда от <strong>Остановка</strong>:</span>
    - <div class="stop-times">...</div>
    - <div class="stop-marks">...</div> (опционально)
    """
    schedules: list[ScheduleBlock] = []

    # Ищем секцию search-bus-times
    times_section_match = re.search(
        r'class="search-bus-times[^"]*"[^>]*>(.*)',
        block, re.DOTALL,
    )
    if not times_section_match:
        return schedules

    times_html = times_section_match.group(1)

    # Разбиваем по меткам "время отъезда от"
    # Каждая метка содержит имя остановки в <strong>
    label_pattern = r'время отъезда от\s*(?:<strong>)?([^<:]+)(?:</strong>)?\s*:'
    labels = list(re.finditer(label_pattern, times_html))

    if not labels:
        # Нет меток — попробуем просто найти stop-times
        times = _parse_times_raw(times_html)
        marks = _parse_marks_raw(times_html)
        if times:
            schedules.append(ScheduleBlock(stop_name="", times=times, special_marks=marks))
        return schedules

    for i, label_m in enumerate(labels):
        stop_name = label_m.group(1).strip()
        start = label_m.end()
        # Определяем конец этого блока — до следующей метки или до конца
        end = labels[i + 1].start() if i + 1 < len(labels) else len(times_html)
        section = times_html[start:end]

        times = _parse_times_raw(section)
        marks = _parse_marks_raw(section)
        schedules.append(ScheduleBlock(stop_name=stop_name, times=times, special_marks=marks))

    return schedules


def _parse_times_raw(html: str) -> list[str]:
    """Извлечь времена из фрагмента HTML содержащего .stop-times."""
    times_match = re.search(
        r'class="stop-times"[^>]*>(.*?)</div>',
        html, re.DOTALL,
    )
    if not times_match:
        return []

    time_spans = re.findall(r'<span[^>]*>([^<]+)</span>', times_match.group(1))
    return [t.strip() for t in time_spans if t.strip()]


def _parse_marks_raw(html: str) -> dict[str, str]:
    """Извлечь спецобозначения из фрагмента HTML содержащего .stop-marks."""
    marks_match = re.search(
        r'class="stop-marks"[^>]*>(.*?)</div>',
        html, re.DOTALL,
    )
    if not marks_match:
        return {}

    text = re.sub(r'<[^>]+>', '', marks_match.group(1)).strip()
    text = re.sub(r'^Обозначения:\s*', '', text)
    marks = {}
    for part in re.split(r';\s*', text):
        part = part.strip()
        m = re.match(r'([A-ZА-Яa-zа-я]+)\s*[-–—]\s*(.+)', part)
        if m:
            marks[m.group(1)] = m.group(2).strip()
    return marks


async def search_routes(
    city_slug: str,
    from_stop: str,
    to_stop: str,
) -> list[KudikinaRoute]:
    """Поиск маршрутов между двумя остановками через kudikina.ru.

    Args:
        city_slug: slug города (например, "omsk")
        from_stop: название остановки отправления
        to_stop: название остановки прибытия

    Returns:
        Список найденных маршрутов с расписаниями
    """
    url = f"{KUDIKINA_BASE}/{city_slug}/search"
    params = {"a": from_stop, "b": to_stop}

    logger.info("Kudikina search: %s → %s (city=%s)", from_stop, to_stop, city_slug)

    async with aiohttp.ClientSession() as session:
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status != 200:
                logger.warning("Kudikina returned status %d", resp.status)
                return []
            html = await resp.text()

    routes = _parse_search_html(html, from_stop)
    logger.info("Kudikina found %d routes", len(routes))
    return routes
