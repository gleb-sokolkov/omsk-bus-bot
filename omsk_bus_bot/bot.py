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
    stream=__import__("sys").stdout,
)
logger = logging.getLogger(__name__)

# Параметры переподключения при потере связи с Telegram API
_RETRY_DELAY_INITIAL = 5      # начальная задержка (сек)
_RETRY_DELAY_MAX = 300         # максимальная задержка (5 мин)
_RETRY_DELAY_MULTIPLIER = 2   # множитель экспоненциального backoff

_scheduler_task: asyncio.Task | None = None


async def on_startup(bot: Bot):
    """Запуск фоновых задач и настройка меню при старте бота."""
    # Меню команд
    try:
        await bot.set_my_commands([
            BotCommand(command="menu", description="Открыть меню"),
        ])
        logger.info("Меню команд установлено")
    except Exception as e:
        logger.warning("Не удалось установить меню команд: %s", e)

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


async def _run_polling():
    """Один цикл polling — создаёт бота, диспатчер и запускает."""
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


async def main():
    """Главный цикл с автоматическим переподключением."""
    if not BOT_TOKEN:
        raise ValueError(
            "BOT_TOKEN не задан. Установите переменную окружения BOT_TOKEN."
        )

    delay = _RETRY_DELAY_INITIAL

    while True:
        try:
            logger.info("Бот запускается...")
            await _run_polling()
            # Нормальный выход (Ctrl+C и т.д.) — прерываем цикл
            break
        except asyncio.CancelledError:
            logger.info("Бот остановлен")
            break
        except KeyboardInterrupt:
            logger.info("Бот остановлен (KeyboardInterrupt)")
            break
        except Exception:
            logger.exception(
                "Polling упал, переподключение через %d сек...", delay
            )
            await asyncio.sleep(delay)
            delay = min(delay * _RETRY_DELAY_MULTIPLIER, _RETRY_DELAY_MAX)
            logger.info("Попытка переподключения...")


if __name__ == "__main__":
    asyncio.run(main())
