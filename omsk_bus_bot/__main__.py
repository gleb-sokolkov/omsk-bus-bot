"""Запуск бота: python -m omsk_bus_bot"""

import asyncio
from .bot import main

asyncio.run(main())
