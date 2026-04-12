"""Конфигурация бота."""

import os
from pathlib import Path

from dotenv import load_dotenv

# Загружаем .env из корня проекта
_env_path = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(_env_path)

# Telegram Bot
BOT_TOKEN = os.getenv("BOT_TOKEN", "")

# 2GIS API
TWOGIS_API_KEY = os.getenv("TWOGIS_API_KEY", "")
TWOGIS_API_URL = "https://routing.api.2gis.com/public_transport/2.0"

# 2GIS Places (Catalog) API — для геокодинга адресов
TWOGIS_PLACES_URL = "https://catalog.api.2gis.com/3.0/items"

# 2GIS Regions API — для определения region_id по названию города
TWOGIS_REGIONS_URL = "https://catalog.api.2gis.com/2.0/region/search"

# Город по умолчанию (название, region_id определяется автоматически через Regions API)
DEFAULT_CITY = os.getenv("DEFAULT_CITY", "Омск")

# Виды транспорта по умолчанию
DEFAULT_TRANSPORT = ["bus"]

# Локаль
DEFAULT_LOCALE = "ru"
PLACES_LOCALE = "ru_RU"

# 2GIS Suggest API — автодополнение названий
TWOGIS_SUGGEST_URL = "https://catalog.api.2gis.com/3.0/suggests"

# Region ID для Suggest API (Омск = 2)
TWOGIS_REGION_ID = int(os.getenv("TWOGIS_REGION_ID", "2"))

# Kudikina.ru — slug города для расписаний
DEFAULT_CITY_SLUG = os.getenv("DEFAULT_CITY_SLUG", "omsk")

# Файл хранилища рейсов
TRIPS_FILE = os.getenv("TRIPS_FILE", "trips.json")
