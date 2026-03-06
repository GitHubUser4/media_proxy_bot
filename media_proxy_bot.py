import asyncio
import os
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from yt_dlp import YoutubeDL

# --- НАСТРОЙКИ ---
load_dotenv()
api_key = os.getenv("API_TOKEN")
DOWNLOAD_DIR = 'downloads'

# Создаем папку для загрузок, если её нет
if not os.path.exists(DOWNLOAD_DIR):
    os.makedirs(DOWNLOAD_DIR)

bot = Bot(token=api_key)
dp = Dispatcher()

def download_insta_video(url):
    """Скачивает видео из Instagram и возвращает путь к файлу."""
    ydl_opts = {
        # Формат: лучшее видео с аудио в одном mp4 файле
        'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
        'outtmpl': f'{DOWNLOAD_DIR}/%(id)s.%(ext)s',
        'quiet': True,
        'no_warnings': True,
    }
    
    with YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        return ydl.prepare_filename(info)

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer("Привет! Пришли мне ссылку на Instagram Reel или пост с видео, и я перешлю его тебе файлом.")

@dp.message(F.text.contains("instagram.com"))
async def handle_instagram(message: types.Message):
    # Извлекаем ссылку из текста (на случай если там есть лишний текст)
    words = message.text.split()
    url = next((w for w in words if "instagram.com" in w), None)

    if not url:
        return

    status_msg = await message.answer("⏳ Обрабатываю Instagram контент...")
    
    try:
        # Запускаем тяжелую загрузку в отдельном потоке, чтобы бот не "тупил"
        loop = asyncio.get_event_loop()
        file_path = await loop.run_in_executor(None, download_insta_video, url)

        # Отправляем видео
        video_file = types.FSInputFile(file_path)
        await message.answer_video(video=video_file, caption="Ваше видео из Instagram")
        
        # Удаляем временный файл
        if os.path.exists(file_path):
            os.remove(file_path)
            
    except Exception as e:
        await message.answer(f"❌ Ошибка при загрузке: {str(e)}")
    finally:
        await status_msg.delete()

async def main():
    # Очистка вебхуков и запуск
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Бот выключен")