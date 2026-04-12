"""Хранилище рейсов в JSON-файле."""

import json
import os
import uuid
from typing import Optional

from .config import TRIPS_FILE
from .models import Trip


class TripStorage:
    """Управление рейсами пользователей через JSON-файл."""

    def __init__(self, filepath: str = TRIPS_FILE):
        self.filepath = filepath
        self._data: dict[str, list[dict]] = {}
        self._load()

    def _load(self):
        if os.path.exists(self.filepath):
            with open(self.filepath, "r", encoding="utf-8") as f:
                self._data = json.load(f)
        else:
            self._data = {}

    def _save(self):
        with open(self.filepath, "w", encoding="utf-8") as f:
            json.dump(self._data, f, ensure_ascii=False, indent=2)

    def _user_key(self, user_id: int) -> str:
        return str(user_id)

    def add_trip(self, user_id: int, trip: Trip) -> Trip:
        """Добавить рейс для пользователя."""
        key = self._user_key(user_id)
        if key not in self._data:
            self._data[key] = []
        if not trip.id:
            trip.id = uuid.uuid4().hex[:8]
        self._data[key].append(trip.to_dict())
        self._save()
        return trip

    def get_trips(self, user_id: int) -> list[Trip]:
        """Получить все рейсы пользователя."""
        key = self._user_key(user_id)
        raw = self._data.get(key, [])
        return [Trip.from_dict(t) for t in raw]

    def get_trip(self, user_id: int, trip_id: str) -> Optional[Trip]:
        """Получить конкретный рейс по ID."""
        trips = self.get_trips(user_id)
        for t in trips:
            if t.id == trip_id:
                return t
        return None

    def delete_trip(self, user_id: int, trip_id: str) -> bool:
        """Удалить рейс."""
        key = self._user_key(user_id)
        trips = self._data.get(key, [])
        new_trips = [t for t in trips if t.get("id") != trip_id]
        if len(new_trips) == len(trips):
            return False
        self._data[key] = new_trips
        self._save()
        return True

    def update_trip(self, user_id: int, trip: Trip) -> bool:
        """Обновить рейс (например, preferred_start_stop)."""
        key = self._user_key(user_id)
        trips = self._data.get(key, [])
        for i, t in enumerate(trips):
            if t.get("id") == trip.id:
                trips[i] = trip.to_dict()
                self._save()
                return True
        return False
