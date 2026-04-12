"""Точка входа бота."""

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand

from .config import BOT_TOKEN
from .handlers import router, storage
from .scheduler import notification_loop

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

_scheduler_task: asyncio.Task | None = None


async def on_startup(bot: Bot):
    """Запуск фоновых задач и настройка меню при старте бота."""
    # Меню команд
    await bot.set_my_commands([
        # Рейсы
        BotCommand(command="add", description="Добавить новый рейс"),
        BotCommand(command="trips", description="Мои рейсы"),
        BotCommand(command="edit", description="Изменить рейс"),
        BotCommand(command="delete", description="Удалить рейс"),
        BotCommand(command="setstops", description="Настроить остановки (фильтр + расписание)"),
        # Уведомления
        BotCommand(command="notify", description="Ежедневные уведомления"),
        BotCommand(command="go", description="Пора на выход"),
        BotCommand(command="setexit", description="Время на выход из дома"),
        # Поиск
        BotCommand(command="route", description="Получить маршрут"),
        BotCommand(command="ksearch", description="Расписание между остановками"),
        # Прочее
        BotCommand(command="help", description="Справка"),
        BotCommand(command="cancel", description="Отменить текущую команду"),
    ])
    logger.info("Меню команд установлено")

    # Планировщик уведомлений
    global _scheduler_task
    _scheduler_task = asyncio.create_task(
        notification_loop(bot, storage, interval=60)
    )
    logger.info("Фоновый планировщик уведомлений запущен")


async def on_shutdown(bot: Bot):
    """Остановка фоновых задач."""
    global _scheduler_task
    if _scheduler_task and not _scheduler_task.done():
        _scheduler_task.cancel()
        try:
            await _scheduler_task
        except asyncio.CancelledError:
            pass
    logger.info("Фоновый планировщик уведомлений остановлен")


async def main():
    if not BOT_TOKEN:
        raise ValueError(
            "BOT_TOKEN не задан. Установите переменную окружения BOT_TOKEN."
        )

    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    logger.info("Бот запускается...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
