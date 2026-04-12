"""Геокодинг адресов через 2GIS Places API (Catalog API 3.0/items)
и определение region_id через 2GIS Regions API."""

import logging
from dataclasses import dataclass
from typing import Optional

import aiohttp

from .config import TWOGIS_API_KEY, TWOGIS_PLACES_URL, TWOGIS_REGIONS_URL, PLACES_LOCALE

logger = logging.getLogger(__name__)

# Кэш region_id: название города -> id
_region_cache: dict[str, int] = {}


@dataclass
class GeoResult:
    """Результат геокодинга — найденный адрес с координатами."""
    name: str
    full_address: str
    lat: float
    lon: float
    item_type: str  # building, street, adm_div и т.д.

    def display(self) -> str:
        """Человекочитаемое представление для inline-кнопки."""
        return f"{self.name} ({self.full_address})"

    def short_display(self) -> str:
        """Короткое представление для текста кнопки."""
        text = self.full_address or self.name
        if len(text) > 60:
            text = text[:57] + "..."
        return text


async def resolve_region_id(city_name: str) -> Optional[int]:
    """
    Определить region_id по названию города через 2GIS Regions API.

    GET https://catalog.api.2gis.com/2.0/region/search?q={city}&key={key}

    Результат кэшируется.
    """
    city_lower = city_name.lower().strip()
    if city_lower in _region_cache:
        return _region_cache[city_lower]

    if not TWOGIS_API_KEY:
        raise ValueError("TWOGIS_API_KEY не задан.")

    params = {
        "key": TWOGIS_API_KEY,
        "q": city_name,
        "locale": PLACES_LOCALE,
    }

    logger.info("Regions API: ищем region_id для '%s'", city_name)

    async with aiohttp.ClientSession() as session:
        async with session.get(TWOGIS_REGIONS_URL, params=params) as resp:
            if resp.status != 200:
                text = await resp.text()
                logger.error("Regions API error %d: %s", resp.status, text)
                return None

            data = await resp.json()

    items = data.get("result", {}).get("items", [])
    if not items:
        logger.warning("Regions API: город '%s' не найден", city_name)
        return None

    # Берём первый результат
    region_id = items[0].get("id")
    region_name = items[0].get("name", city_name)
    logger.info("Regions API: '%s' -> region_id=%s", region_name, region_id)

    if region_id is not None:
        _region_cache[city_lower] = region_id

    return region_id


async def geocode(query: str, region_id: int = None, city_name: str = None, limit: int = 5) -> list[GeoResult]:
    """
    Поиск адреса через 2GIS Places API.

    Args:
        query: Текстовый адрес или название места.
        region_id: ID региона 2GIS (если известен).
        city_name: Название города (если region_id не задан, определится автоматически).
        limit: Максимальное количество результатов.

    Returns:
        Список GeoResult с координатами.
    """
    if not TWOGIS_API_KEY:
        raise ValueError("TWOGIS_API_KEY не задан.")

    # Определяем region_id, если не передан явно
    if region_id is None and city_name:
        region_id = await resolve_region_id(city_name)
        if region_id is None:
            raise ValueError(f"Не удалось определить region_id для города «{city_name}»")

    params = {
        "key": TWOGIS_API_KEY,
        "q": query,
        "locale": PLACES_LOCALE,
        "fields": "items.point,items.full_address_name",
        "page_size": limit,
        "type": "building,street,adm_div,station,crossroad,attraction",
    }
    if region_id is not None:
        params["region_id"] = region_id

    logger.info("Геокодинг запрос: '%s' (region_id=%s)", query, region_id)

    async with aiohttp.ClientSession() as session:
        async with session.get(TWOGIS_PLACES_URL, params=params) as resp:
            if resp.status != 200:
                text = await resp.text()
                logger.error("Places API error %d: %s", resp.status, text)
                raise ConnectionError(f"Ошибка Places API: HTTP {resp.status}")

            data = await resp.json()

    items = data.get("result", {}).get("items", [])
    results = []

    for item in items:
        point = item.get("point")
        if not point:
            continue

        name = item.get("name", "")
        full_address = item.get("full_address_name", item.get("address_name", name))
        lat = point.get("lat")
        lon = point.get("lon")
        item_type = item.get("type", "unknown")

        if lat is not None and lon is not None:
            results.append(GeoResult(
                name=name,
                full_address=full_address,
                lat=lat,
                lon=lon,
                item_type=item_type,
            ))

    logger.info("Геокодинг: найдено %d результатов для '%s'", len(results), query)
    return results
