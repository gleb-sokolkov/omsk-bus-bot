"""Обогащение маршрутов 2GIS расписаниями из kudikina.ru.

Принцип работы:
1. Из ответа 2GIS извлекаем: остановку посадки, конечную остановку, номер автобуса.
2. Запрашиваем kudikina.ru по этим остановкам.
3. Находим совпадение по номеру автобуса.
4. Подменяем schedules в RouteInfo — все остальные методы работают как прежде.
"""

import logging
import re
from typing import Optional

from .config import DEFAULT_CITY_SLUG
from .kudikina_client import search_routes as kudikina_search, KudikinaRoute
from .models import RouteInfo

logger = logging.getLogger(__name__)


def _normalize_bus_number(number: str) -> str:
    """Нормализовать номер автобуса для сравнения.

    "24" -> "24", "470Н" -> "470н", "24К" -> "24к"
    """
    return number.strip().lower()


def _match_bus_number(twogis_number: str, kudikina_number: str) -> bool:
    """Нечёткое сравнение номеров автобуса.

    Примеры:
    - "24" == "24" → True
    - "470Н" == "470н" → True
    - "24" == "24К" → False (разные маршруты)
    - "500" == "500" → True
    """
    a = _normalize_bus_number(twogis_number)
    b = _normalize_bus_number(kudikina_number)
    return a == b


def _find_matching_kudikina_route(
    kudikina_routes: list[KudikinaRoute],
    bus_numbers: list[str],
) -> Optional[KudikinaRoute]:
    """Найти маршрут kudikina, совпадающий по номеру автобуса."""
    for bus_num in bus_numbers:
        for kr in kudikina_routes:
            if _match_bus_number(bus_num, kr.number):
                return kr
    return None


async def enrich_with_kudikina(
    route: RouteInfo,
    city_slug: str = None,
) -> RouteInfo:
    """Подменить расписание 2GIS на точное расписание из kudikina.ru.

    Если kudikina недоступна или автобус не найден — возвращает route без изменений
    (graceful degradation).

    Args:
        route: маршрут от 2GIS API
        city_slug: slug города для kudikina (по умолчанию из config)

    Returns:
        Тот же RouteInfo с обновлённым schedules (или без изменений при ошибке)
    """
    if city_slug is None:
        city_slug = DEFAULT_CITY_SLUG

    segments = route.extract_passage_info()
    if not segments:
        return route

    # Берём первый транспортный сегмент (основной автобус)
    seg = segments[0]
    start_stop = seg["start_stop"]
    end_stop = seg["end_stop"]
    bus_numbers = seg["bus_numbers"]

    if not start_stop or not end_stop or not bus_numbers:
        logger.debug("Недостаточно данных для обогащения: start=%s end=%s bus=%s",
                     start_stop, end_stop, bus_numbers)
        return route

    # Запрашиваем kudikina
    try:
        kudikina_routes = await kudikina_search(city_slug, start_stop, end_stop)
    except Exception as e:
        logger.warning("Kudikina недоступна, оставляем 2GIS расписание: %s", e)
        return route

    if not kudikina_routes:
        # Попробуем с менее точным end_stop — возьмём промежуточную остановку
        all_stops = seg["all_stops"]
        if len(all_stops) > 2:
            # Пробуем с серединой маршрута
            mid_stop = all_stops[len(all_stops) // 2]
            try:
                kudikina_routes = await kudikina_search(city_slug, start_stop, mid_stop)
            except Exception:
                pass

    if not kudikina_routes:
        logger.debug("Kudikina: маршруты от '%s' до '%s' не найдены", start_stop, end_stop)
        return route

    # Ищем совпадение по номеру
    matched = _find_matching_kudikina_route(kudikina_routes, bus_numbers)
    if not matched:
        logger.debug("Kudikina: автобус %s не найден среди %d маршрутов",
                     bus_numbers, len(kudikina_routes))
        return route

    # Конвертируем расписание kudikina → формат 2GIS
    new_schedules = matched.to_2gis_schedules()
    if not new_schedules:
        logger.debug("Kudikina: пустое расписание для автобуса %s", matched.number)
        return route

    # Подменяем!
    old_count = len(route.schedules)
    route.schedules = new_schedules
    logger.info(
        "Расписание обогащено: автобус %s, остановка '%s' → '%s', "
        "было %d записей (2GIS) → стало %d (kudikina)",
        matched.number, start_stop, end_stop, old_count, len(new_schedules),
    )

    return route


async def enrich_routes(
    routes: list[RouteInfo],
    city_slug: str = None,
) -> list[RouteInfo]:
    """Обогатить список маршрутов расписаниями из kudikina.

    Удобная обёртка для обогащения всех маршрутов разом.
    """
    enriched = []
    for route in routes:
        enriched.append(await enrich_with_kudikina(route, city_slug))
    return enriched
