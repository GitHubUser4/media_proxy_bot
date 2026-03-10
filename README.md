---

# 🤖 Media Proxy Bot

Telegram-бот для автоматического скачивания, сжатия и пересылки видео из Instagram (Reels, Posts) напрямую в чат. Идеально подходит для обхода ограничений просмотра и сохранения контента.

---

## 📋 Требования

* **Python:** 3.10 или выше
* **FFmpeg:** Необходим для процесса сжатия видео
* **Instagram Cookies:** В формате **Netscape** (используйте расширения типа *Get Cookies.txt* для браузера)

---

## 🛠 Установка на VPS (Ubuntu/Debian)

### 1. Подготовка системы

Обновите пакеты и установите необходимые системные зависимости:

```bash
sudo apt update && sudo apt install ffmpeg python3-pip python3-venv -y

```

### 2. Клонирование и настройка

Создайте директорию проекта и настройте виртуальное окружение:

```bash
mkdir media_proxy_bot && cd media_proxy_bot
python3 -m venv venv
source venv/bin/activate
pip install aiogram yt-dlp python-dotenv psutil

```

### 3. Размещение файлов

Поместите в папку `/home/USER/media_proxy_bot` следующие компоненты:

* `media_proxy_bot.py` — основной код бота.
* `instagram_cookies.txt` — ваши куки из браузера.
* `.env` — файл с настройками (содержимое: `BOT_TOKEN=ваш_токен`).

---

## ⚙️ Автозапуск через Systemd

Чтобы бот работал 24/7 и автоматически перезапускался при сбоях или перезагрузке сервера, настройте его как системную службу.

### 1. Создание файла службы

```bash
sudo nano /etc/systemd/system/media_proxy_bot.service

```

### 2. Конфигурация

Вставьте следующее содержимое, заменив **`USER`** на ваше реальное имя пользователя в системе:

```ini
[Unit]
Description=Media Proxy Telegram Bot
After=network.target

[Service]
# Путь к папке с ботом
WorkingDirectory=/home/USER/media_proxy_bot
# Путь к python внутри venv и путь к скрипту
ExecStart=/home/USER/media_proxy_bot/venv/bin/python3 /home/USER/media_proxy_bot/media_proxy_bot.py
Restart=always
RestartSec=5
User=USER

[Install]
WantedBy=multi-user.target

```

### 3. Активация службы

Выполните команды по очереди для регистрации и запуска:

```bash
sudo systemctl daemon-reload          # Обновить список служб
sudo systemctl enable media_proxy_bot    # Включить автозапуск
sudo systemctl start media_proxy_bot     # Запустить бота сейчас

```

---

## 📝 Полезные команды

| Действие | Команда |
| --- | --- |
| **Проверить статус** | `sudo systemctl status media_proxy_bot` |
| **Посмотреть логи** | `tail -f proxy_bot.log` |
| **Перезапуск** | `sudo systemctl restart media_proxy_bot` |
| **Остановка** | `sudo systemctl stop media_proxy_bot` |

---

## ⚠️ Решение проблем

* **Ошибка "Login Required":** Instagram отозвал сессию. Обновите файл `instagram_cookies.txt`, экспортировав свежие куки из браузера.
* **Видео не сжимается:** Убедитесь, что FFmpeg установлен корректно, выполнив команду `ffmpeg -version` в терминале.
* **Бот не отвечает:** 1. Проверьте правильность токена в `.env`.
2. Убедитесь, что не запущен другой экземпляр бота.
3. Проверьте `bot_log.txt` на наличие ошибок API.

---
