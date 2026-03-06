🤖 Media Proxy Bot
Telegram-бот для автоматического скачивания, сжатия и пересылки видео из Instagram (Reels, Posts) в чат.

📋 Требования
Python 3.10+

FFmpeg (для сжатия видео)

Instagram Cookies (в формате Netscape)

🛠 Установка на VPS (Ubuntu/Debian)
1. Подготовка системы

Обновите пакеты и установите FFmpeg:

Bash
sudo apt update && sudo apt install ffmpeg python3-pip python3-venv -y
2. Клонирование и настройка

Создайте папку проекта и настройте виртуальное окружение:

Bash
mkdir media_proxy_bot && cd media_proxy_bot
python3 -m venv venv
source venv/bin/activate
pip install aiogram yt-dlp
3. Файлы проекта

Поместите в папку /home/USER/media_proxy_bot следующие файлы:

media_proxy_bot.py (сам код бота)

instagram_cookies.txt (ваши куки из браузера)

.env (создайте файл и пропишите там BOT_TOKEN=ваш_токен, либо вставьте токен прямо в код)

⚙️ Запуск как службы (Systemd)
Чтобы бот работал 24/7 и сам поднимался после сбоев, настроим его как системную службу.

1. Создайте файл службы

Bash
sudo nano /etc/systemd/system/media_proxy_bot.service
2. Вставьте следующее содержимое (замените USER на ваше имя пользователя):

Ini, TOML
[Unit]
Description=Media Proxy Telegram Bot
After=network.target

[Service]
# Путь к папке с ботом
WorkingDirectory=/home/USER/media_proxy_bot
# Путь к python внутри виртуального окружения и путь к скрипту
ExecStart=/home/USER/media_proxy_bot/venv/bin/python3 /home/USER/media_proxy_bot/media_proxy_bot.py
Restart=always
RestartSec=5
User=USER

[Install]
WantedBy=multi-user.target
3. Активация службы

Выполните команды по очереди:

Bash
sudo systemctl daemon-reload      # Обновить список служб
sudo systemctl enable media_proxy_bot    # Включить автозапуск при старте системы
sudo systemctl start media_proxy_bot     # Запустить бота прямо сейчас
📝 Полезные команды
Проверить статус бота:

Bash
sudo systemctl status media_proxy_bot
Посмотреть логи (в реальном времени):

Bash
tail -f bot_log.txt
Перезапустить бота (после обновления кода или куков):

Bash
sudo systemctl restart media_proxy_bot
⚠️ Решение проблем
Ошибка "Login Required": Обновите файл instagram_cookies.txt (экспортируйте новые куки из браузера).

Видео не сжимается: Убедитесь, что команда ffmpeg -version работает в консоли.

Бот не отвечает: Проверьте bot_log.txt на наличие ошибок токена или конфликта вебхуков.
