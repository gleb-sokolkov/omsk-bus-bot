# Развёртывание omsk-bus-bot на macOS

## 1. Клонирование и настройка

```bash
cd ~/tools
git clone <repo-url> omsk-bus-bot
cd omsk-bus-bot

# Виртуальное окружение
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Переменные окружения
cp .env.example .env   # или создать вручную
nano .env
```

Содержимое `.env`:
```
BOT_TOKEN=<токен от @BotFather>
TWOGIS_API_KEY=<ключ 2GIS>
TWOGIS_REGION_ID=2
DEFAULT_CITY=Омск
DEFAULT_CITY_SLUG=omsk
```

## 2. Проверка запуска

```bash
source venv/bin/activate
python -m omsk_bus_bot
```

Бот должен залогировать `Бот запускается...` и начать polling. Ctrl+C для остановки.

## 3. Установка как фоновый сервис (launchd)

```bash
# Создать папку для логов
mkdir -p ~/tools/omsk-bus-bot/logs

# Скопировать plist
cp com.omsk-bus-bot.plist ~/Library/LaunchAgents/

# Загрузить сервис
launchctl load ~/Library/LaunchAgents/com.omsk-bus-bot.plist
```

Бот запустится автоматически и будет перезапускаться при падении (`KeepAlive`).

## 4. Управление сервисом

```bash
# Остановить
launchctl unload ~/Library/LaunchAgents/com.omsk-bus-bot.plist

# Запустить
launchctl load ~/Library/LaunchAgents/com.omsk-bus-bot.plist

# Перезапустить (остановить + запустить)
launchctl unload ~/Library/LaunchAgents/com.omsk-bus-bot.plist
launchctl load ~/Library/LaunchAgents/com.omsk-bus-bot.plist

# Проверить статус
launchctl list | grep omsk
```

## 5. Логи

```bash
# Stdout (основной лог)
tail -f ~/tools/omsk-bus-bot/logs/bot.log

# Stderr (ошибки)
tail -f ~/tools/omsk-bus-bot/logs/bot.err
```

## 6. Устойчивость к сбоям

Бот имеет два уровня защиты от потери связи с Telegram API:

- **Уровень 1 (aiogram)**: polling внутри aiogram обрабатывает временные ошибки сети.
- **Уровень 2 (bot.py)**: если polling падает полностью, внешний цикл в `main()` перезапускает его с экспоненциальным backoff (5 сек → 10 → 20 → ... → макс 5 мин).
- **Уровень 3 (launchd)**: `KeepAlive=true` перезапускает процесс, если он завершился.

При блокировке Telegram бот будет тихо ждать и пытаться переподключиться. Когда VPN восстановит доступ — бот автоматически продолжит работу.

## 7. Обновление кода

```bash
cd ~/tools/omsk-bus-bot
git pull

# Перезапуск сервиса
launchctl unload ~/Library/LaunchAgents/com.omsk-bus-bot.plist
launchctl load ~/Library/LaunchAgents/com.omsk-bus-bot.plist
```
