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
MAX_SIZE_MB = 45  # Порог, после которого нужно жесткое сжатие

if not os.path.exists(DOWNLOAD_DIR):
    os.makedirs(DOWNLOAD_DIR)

bot = Bot(token=api_key)
dp = Dispatcher()

def get_file_size(file_path):
    return os.path.getsize(file_path) / (1024 * 1024)

def compress_video(input_path, force_heavy=False):
    """
    Сжатие видео. 
    force_heavy=True используется, если файл все еще слишком большой.
    """
    output_path = input_path.replace(".mp4", "_processed.mp4")
    
    # Настройки качества: 28 — норма, 35 — сильное сжатие (хуже качество, меньше вес)
    crf_value = "35" if force_heavy else "28"
    
    command = [
        'ffmpeg', '-y', '-i', input_path,
        '-vcodec', 'libx264', '-crf', crf_value, 
        '-preset', 'veryfast', # Быстрый пресет важен для слабых VPS
        '-acodec', 'aac', '-b:a', '128k',
        output_path
    ]
    
    try:
        subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return output_path
    except Exception as e:
        logging.error(f"FFmpeg error: {e}")
        return input_path

def download_instagram(url):
    ydl_opts = {
        'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
        'outtmpl': f'{DOWNLOAD_DIR}/%(id)s.%(ext)s',
        'cookiefile': 'instagram_cookies.txt', # Не забудьте загрузить файл на сервер!
        'quiet': True,
    }
    with YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        return ydl.prepare_filename(info)

@dp.message(F.text.contains("instagram.com"))
async def handle_instagram(message: types.Message):
    url = next((w for w in message.text.split() if "instagram.com" in w), None)
    if not url: return

    status = await message.answer("📥 Начинаю работу...")
    
    try:
        loop = asyncio.get_event_loop()
        
        # 1. Загрузка
        raw_file = await loop.run_in_executor(None, download_instagram, url)
        
        # 2. Проверка размера и сжатие
        file_size = get_file_size(raw_file)
        await status.edit_text(f"⚙️ Видео ({file_size:.1f}MB) обрабатывается...")
        
        # Сжимаем в любом случае для оптимизации, но если файл > 45MB — жмем сильно
        final_file = await loop.run_in_executor(None, compress_video, raw_file, file_size > MAX_SIZE_MB)

        # 3. Отправка
        video_file = types.FSInputFile(final_file)
        await message.answer_video(video=video_file)
        
        # Чистим файлы
        for f in {raw_file, final_file}:
            if os.path.exists(f): os.remove(f)

    except Exception as e:
        await message.answer("❌ Ошибка. Возможно, видео слишком длинное или профиль закрыт.")
    finally:
        await status.delete()

async def main():
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main())
