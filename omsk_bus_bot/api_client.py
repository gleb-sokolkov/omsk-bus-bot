"""Клиент для 2GIS Public Transport API."""

import logging
from typing import Optional

import aiohttp

from .config import (
    TWOGIS_API_KEY,
    TWOGIS_API_URL,
    TWOGIS_SUGGEST_URL,
    DEFAULT_TRANSPORT,
    DEFAULT_LOCALE,
    PLACES_LOCALE,
)
from .models import RouteInfo

logger = logging.getLogger(__name__)


def _extract_start_stop_name(movements: list) -> Optional[str]:
    """Извлечь название первой остановки посадки (passage) из movements."""
    for m in movements:
        if m.get("type") == "passage":
            waypoint = m.get("waypoint", {})
            return waypoint.get("name")
    return None


def _parse_route_item(item: dict) -> RouteInfo:
    """Парсинг одного варианта маршрута из ответа API."""
    movements = item.get("movements", [])
    start_stop = _extract_start_stop_name(movements)

    return RouteInfo(
        route_id=item.get("route_id", item.get("id", "")),
        total_duration=item.get("total_duration", 0),
        total_distance=item.get("total_distance", 0),
        transfer_count=item.get("transfer_count", 0),
        crossing_count=item.get("crossing_count", 0),
        pedestrian=item.get("pedestrian", False),
        total_walkway_distance=item.get("total_walkway_distance", ""),
        movements=movements,
        schedules=item.get("schedules") or [],
        start_stop_name=start_stop,
    )


async def fetch_routes(
    start_lat: float,
    start_lon: float,
    end_lat: float,
    end_lon: float,
    start_name: Optional[str] = None,
    end_name: Optional[str] = None,
    transport: Optional[list[str]] = None,
    max_results: int = 3,
) -> list[RouteInfo]:
    """
    Запросить маршруты общественного транспорта из 2GIS API.

    Returns:
        Список RouteInfo с вариантами маршрутов.
    """
    if not TWOGIS_API_KEY:
        raise ValueError("TWOGIS_API_KEY не задан. Установите переменную окружения.")

    payload = {
        "locale": DEFAULT_LOCALE,
        "enable_schedule": True,
        "source": {
            "point": {"lat": start_lat, "lon": start_lon},
        },
        "target": {
            "point": {"lat": end_lat, "lon": end_lon},
        },
        "transport": transport or DEFAULT_TRANSPORT,
        "max_result_count": max_results,
    }

    if start_name:
        payload["source"]["name"] = start_name
    if end_name:
        payload["target"]["name"] = end_name

    url = f"{TWOGIS_API_URL}?key={TWOGIS_API_KEY}"

    logger.info("Запрос к 2GIS API: %s", payload)

    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload) as resp:
            if resp.status == 204:
                logger.info("2GIS API: маршрут не найден (204)")
                return []
            if resp.status == 422:
                error = await resp.json()
                logger.error("2GIS API error 422: %s", error)
                raise ValueError(f"Ошибка запроса: {error.get('message', 'неизвестная ошибка')}")
            if resp.status != 200:
                text = await resp.text()
                logger.error("2GIS API error %d: %s", resp.status, text)
                raise ConnectionError(f"Ошибка API: HTTP {resp.status}")

            data = await resp.json()

    # Ответ — массив вариантов маршрутов (может быть вложенный массив)
    routes = []
    if isinstance(data, list):
        for item in data:
            if isinstance(item, list):
                for sub in item:
                    routes.append(_parse_route_item(sub))
            elif isinstance(item, dict):
                routes.append(_parse_route_item(item))

    logger.info("Получено %d вариантов маршрутов", len(routes))
    return routes


def filter_by_start_stop(routes: list[RouteInfo], stop_name: str) -> list[RouteInfo]:
    """Оставить маршруты, у которых первый транспортный сегмент начинается с stop_name."""
    if not stop_name:
        return routes
    stop_lower = stop_name.strip().lower()
    filtered = []
    for r in routes:
        segments = r.extract_passage_info()
        if segments:
            first_start = segments[0].get("start_stop", "").strip().lower()
            if stop_lower == first_start:
                filtered.append(r)
    return filtered or routes  # fallback: если ничего не прошло — вернуть все


async def suggest_stops(
    query: str,
    region_id: Optional[int] = None,
) -> list[str]:
    """Автодополнение названий остановок через 2GIS Suggest API.

    Args:
        query: текст, введённый пользователем (например "Студ").
        region_id: ID региона в 2GIS (если None — ищет глобально).

    Returns:
        Список уникальных названий-подсказок (до 5 штук).
    """
    if not TWOGIS_API_KEY or not query.strip():
        return []

    params = {
        "key": TWOGIS_API_KEY,
        "q": query.strip(),
        "locale": PLACES_LOCALE,
        "type": "station",
        "page_size": 10,
    }
    if region_id:
        params["region_id"] = region_id

    try:
        async with aiohttp.ClientSession() as session:
            logger.info("Suggest API request: %s params=%s", TWOGIS_SUGGEST_URL, {k: v for k, v in params.items() if k != "key"})
            async with session.get(TWOGIS_SUGGEST_URL, params=params) as resp:
                data = await resp.json()
                logger.info("Suggest API response status=%d, body=%s", resp.status, data)
                if resp.status != 200:
                    logger.warning("2GIS Suggest error %d", resp.status)
                    return []

        items = data.get("result", {}).get("items", [])
        names = []
        seen = set()
        for item in items:
            name = item.get("name", "").strip()
            if not name or name in seen:
                continue
            seen.add(name)
            names.append(name)
            if len(names) >= 5:
                break
        return names
    except Exception as e:
        logger.warning("Ошибка Suggest API: %s", e)
        return []
