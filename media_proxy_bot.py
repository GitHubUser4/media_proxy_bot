import asyncio
import os
import subprocess
import logging
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from yt_dlp import YoutubeDL

# Настройка логирования, чтобы видеть ошибки в консоли
# logging.basicConfig(level=logging.INFO)

# --- НАСТРОЙКИ ---
load_dotenv()
api_key = os.getenv("API_TOKEN")
DOWNLOAD_DIR = 'downloads'

if not os.path.exists(DOWNLOAD_DIR):
    os.makedirs(DOWNLOAD_DIR)

bot = Bot(token=api_key)
dp = Dispatcher()

def compress_video(input_path):
    """Сжатие видео через FFmpeg (Кодек H.264)"""
    output_path = input_path.replace(".mp4", "_processed.mp4")
    # -crf 28: оптимальный баланс между весом и качеством
    # -preset veryfast: чтобы сервер не висел долго на одной задаче
    command = [
        'ffmpeg', '-y', '-i', input_path,
        '-vcodec', 'libx264', '-crf', '28', 
        '-preset', 'veryfast', '-acodec', 'aac', '-b:a', '128k',
        output_path
    ]
    try:
        subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return output_path
    except Exception as e:
        logging.error(f"Ошибка FFmpeg: {e}")
        return input_path

def download_instagram(url):
    """Загрузка контента из Instagram с использованием Cookies"""
    ydl_opts = {
        'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
        'outtmpl': f'{DOWNLOAD_DIR}/%(id)s.%(ext)s',
        'quiet': True,
        'no_warnings': True,
        # Путь к файлу с куками
        'cookiefile': 'instagram_cookies.txt', 
        'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    }
    with YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        return ydl.prepare_filename(info)

@dp.message(F.text.contains("instagram.com"))
async def handle_instagram(message: types.Message):
    # Извлекаем чистую ссылку из сообщения
    url = next((word for word in message.text.split() if "instagram.com" in word), None)
    if not url: return

    status = await message.answer("🛸 Вижу ссылку. Начинаю магию...")
    
    try:
        loop = asyncio.get_event_loop()
        
        # Шаг 1: Загрузка
        await status.edit_text("📥 Скачиваю контент из Instagram...")
        raw_file = await loop.run_in_executor(None, download_instagram, url)
        
        # Шаг 2: Сжатие
        await status.edit_text("⚙️ Оптимизирую видео и сжимаю размер...")
        final_file = await loop.run_in_executor(None, compress_video, raw_file)

        # Шаг 3: Отправка
        await status.edit_text("📤 Отправляю в чат...")
        video = types.FSInputFile(final_file)
        await message.answer_video(video=video, caption="Готово! 🎬")
        
        # Удаление временных файлов
        for f in {raw_file, final_file}:
            if os.path.exists(f): os.remove(f)

    except Exception as e:
        logging.error(f"General Error: {e}")
        await message.answer("⚠️ Не удалось обработать ссылку. Возможно, профиль закрыт или видео слишком длинное.")
    finally:
        await status.delete()

async def main():
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
