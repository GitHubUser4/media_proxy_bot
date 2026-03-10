import os
import asyncio
import logging
import random
import shutil
import requests
import time
import subprocess
import json
import re
import psutil
import platform
from urllib.parse import urlparse, urlunparse
from dotenv import load_dotenv
from datetime import datetime
from aiogram import Bot, Dispatcher, Router, types, F
from aiogram.types import FSInputFile, InputMediaPhoto, Message
from aiogram.filters import Command
from aiogram.utils.media_group import MediaGroupBuilder
from playwright.async_api import async_playwright
from yt_dlp import YoutubeDL

# --- 1. ЗАГРУЗКА КОНФИГУРАЦИИ ---
load_dotenv()
api_key = os.getenv("API_TOKEN")
admin_id = int(os.getenv("ADMIN_ID", 0))
COOKIE_FILE = "instagram_cookies.txt"
TEMP_BASE_DIR = "downloads"
LOG_FILE = "proxy_bot.log"
MAX_SIZE_MB = 48 

browser_semaphore = asyncio.Semaphore(1)
cached_cookies = None
last_cookie_update = 0

# --- 2. НАСТРОЙКА ЛОГИРОВАНИЯ ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE, encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

if not api_key:
    logger.critical("API_TOKEN не найден в .env файле!")
    exit(1)

bot = Bot(token=api_key)
dp = Dispatcher()

# --- 3. ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---
async def load_cookies_to_context(context, file_path):
    if not os.path.exists(file_path): return
    try:
        # Самый дубовый метод: грузим только имя и значение
        ydl = YoutubeDL({'cookiefile': file_path, 'quiet': True})
        for cookie in ydl.cookiejar:
            try:
                await context.add_cookies([{
                    'name': cookie.name,
                    'value': cookie.value,
                    'domain': cookie.domain,
                    'path': cookie.path,
                    'secure': True if cookie.secure else False
                }])
            except: continue
        logger.info("✅ Куки впрыснуты (упрощенно)")
    except Exception as e:
        logger.error(f"Ошибка кук: {e}")

def get_video_dimensions(file_path):
    cmd = ['ffprobe', '-v', 'quiet', '-print_format', 'json', '-show_streams', file_path]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        data = json.loads(result.stdout)
        for stream in data.get('streams', []):
            if stream.get('codec_type') == 'video':
                return int(stream.get('width')), int(stream.get('height'))
    except: pass
    return None, None

def fix_video_for_telegram(input_path, output_path):
    command = [
        'ffmpeg', '-y', '-i', input_path,
        '-vf', 'scale=trunc(iw/2)*2:trunc(ih/2)*2,setsar=1', 
        '-vcodec', 'libx264', '-crf', '30', '-preset', 'superfast', 
        '-pix_fmt', 'yuv420p', '-map_metadata', '-1', 
        '-acodec', 'aac', '-b:a', '96k', '-movflags', '+faststart',
        output_path
    ]
    try:
        subprocess.run(command, check=True, capture_output=True)
        return output_path
    except:
        return input_path

def clean_url(raw_url):
    parsed = urlparse(raw_url)
    return urlunparse(parsed._replace(query=""))

# --- 4. ЛОГИКА ЗАГРУЗКИ ---

def download_content(url, temp_path):
    ydl_opts = {
        'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
        'outtmpl': os.path.join(temp_path, 'media_%(id)s.%(ext)s'),
        'cookiefile': COOKIE_FILE if os.path.exists(COOKIE_FILE) else None,
        'quiet': True,
        'merge_output_format': 'mp4',
    }
    with YoutubeDL(ydl_opts) as ydl:
        return ydl.extract_info(url, download=True)

async def download_insta_media_playwright(url, temp_dir):
    async with browser_semaphore:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True) 
            context = await browser.new_context(viewport={'width': 1280, 'height': 1440})
            await load_cookies_to_context(context, COOKIE_FILE)
            page = await context.new_page()

            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=60000)
                await asyncio.sleep(5)
                # --- ПРОВЕРКА АВТОРИЗАЦИИ ---
                current_url = page.url
                if "login" in current_url:
                    logger.error("❌ Куки не сработали: Инстаграм перенаправил на страницу логина")
                    await browser.close()
                    return [], "Ошибка авторизации (нужны свежие куки)"
                # ----------------------------
                # --- НОВОЕ: ДОСТАЕМ ТЕКСТ ПОСТА ---
                post_text = ""
                try:
                    # Ищем текст поста в заголовке h1 (стандарт Инсты) или в мета-тегах
                    post_text = await page.evaluate("""() => {
                        const h1 = document.querySelector('article h1');
                        if (h1) return h1.innerText;
                        const meta = document.querySelector('meta[property="og:title"]');
                        return meta ? meta.content : '';
                    }""")
                    # Убираем мусорную приписку вроде "Toyota on Instagram: "
                    if 'on Instagram: "' in post_text:
                        post_text = post_text.split('on Instagram: "')[-1].rstrip('"')
                except Exception as e:
                    logger.error(f"Не удалось получить текст: {e}")
                # -----------------------------------

                saved_files = []
                all_media_urls = set()

                for slide in range(10):
                    await asyncio.sleep(2)
                    elements = await page.query_selector_all("video, img")
                    
                    for el in elements:
                        src = await el.get_attribute("src")
                        if not src or src in all_media_urls: continue
                        
                        box = await el.bounding_box()
                        if not box or box['width'] < 300: continue
                        if box['y'] > 600: continue # Наш спасительный фильтр

                        all_media_urls.add(src)
                        tag = await el.evaluate("node => node.tagName")
                        ext = "mp4" if tag == 'VIDEO' else "jpg"
                        file_path = os.path.join(temp_dir, f"media_{len(all_media_urls)}.{ext}")

                        try:
                            r = requests.get(src, timeout=10)
                            if r.status_code == 200:
                                with open(file_path, "wb") as f:
                                    f.write(r.content)
                                saved_files.append(file_path)
                                logger.info(f"✅ Файл сохранен: {file_path}")
                        except: continue

                    next_btn = await page.query_selector("button[aria-label*='Next'], button[aria-label*='Далее'], ._af6z")
                    if next_btn and await next_btn.is_visible():
                        await next_btn.click()
                    else:
                        break
                
                await browser.close()
                # ВАЖНО: теперь возвращаем два значения
                return saved_files, post_text 
            except Exception as e:
                logger.error(f"Ошибка: {e}")
                await browser.close()
                return [], ""

# --- 5. ОБРАБОТЧИК СООБЩЕНИЙ ---
@dp.message(Command("status"))
async def admin_status(message: Message):
    # Временный лог в консоль VPS, чтобы увидеть, кто пишет
    print(f"DEBUG: Получена команда /status от ID {message.from_user.id}")
    print(f"DEBUG: ID from .env - {admin_id}")
    # Если пишет не админ — полная тишина (бэкдор)
    if message.from_user.id != admin_id:
        return 

    # Проверка кук
    cookie_info = "❌ Файл не найден"
    if os.path.exists(COOKIE_FILE):
        mtime = os.path.getmtime(COOKIE_FILE)
        # Показывает дату последнего изменения файла
        dt = datetime.fromtimestamp(mtime).strftime('%d.%m %H:%M')
        cookie_info = f"✅ Обновлены: {dt}"

    # Ресурсы сервера
    ram = psutil.virtual_memory()
    disk = psutil.disk_usage('/')
    
    status_text = (
        "<b>🛠 Системный статус:</b>\n\n"
        f"<b>🍪 Куки:</b> {cookie_info}\n"
        f"<b>🧠 RAM:</b> {ram.percent}% ({ram.used // (1024**2)}MB)\n"
        f"<b>💾 Диск:</b> {disk.free // (1024**3)}GB свободно\n"
        f"<b>🕒 Время VPS:</b> {datetime.now().strftime('%H:%M:%S')}"
    )

    await message.answer(status_text, parse_mode="HTML")
    
@dp.message(F.text.contains("instagram.com"))
async def handle_instagram(message: types.Message):
    url_match = re.search(r'(https?://[^\s]+)', message.text)
    if not url_match: return
    
    url = clean_url(url_match.group(1))
    status_msg = await message.answer("🚀 Танк поехал за медиа...")
    
    temp_dir = os.path.join(TEMP_BASE_DIR, f"temp_{message.from_user.id}_{int(time.time())}")
    os.makedirs(temp_dir, exist_ok=True)

    try:
        post_caption = "" # Переменная для текста
        
        # План А
        success = False
        try:
            await status_msg.edit_text("🔍 Пробую быстрый метод...")
            loop = asyncio.get_event_loop()
            info = await loop.run_in_executor(None, download_content, url, temp_dir)
            if info:
                # Берем описание из yt-dlp, если сработал быстрый метод
                post_caption = info.get('description', '') or info.get('title', '')
            if os.listdir(temp_dir): success = True
        except Exception as e:
            logger.warning(f"yt-dlp failed: {e}")

        # План Б
        if not success:
            await status_msg.edit_text("🌐 Запускаю браузер (План Б)...")
            # Распаковываем наши два значения из обновленной функции
            files_from_pw, post_caption = await download_insta_media_playwright(url, temp_dir)

        downloaded_files = [f for f in os.listdir(temp_dir) if not f.startswith('fixed_')]
        if not downloaded_files:
            raise Exception("Не удалось получить медиа.")

        await status_msg.edit_text("⚙️ Обработка...")
        processed_paths = []
        for file_name in downloaded_files:
            path = os.path.join(temp_dir, file_name)
            if file_name.lower().endswith(('.mp4', '.mov')):
                out = os.path.join(temp_dir, f"fixed_{file_name}")
                processed_paths.append(fix_video_for_telegram(path, out))
            else:
                processed_paths.append(path)

        # Ограничиваем длину текста для Telegram (лимит 1024 символа для медиа)
        clean_caption = post_caption[:1024] if post_caption else ""

        await status_msg.edit_text("📤 Отправка...")
        
        # Формируем отправку только с описанием
        if len(processed_paths) == 1:
            p = processed_paths[0]
            if p.lower().endswith(('.mp4', '.mov')):
                w, h = get_video_dimensions(p)
                await message.answer_video(
                    FSInputFile(p), 
                    width=w, 
                    height=h, 
                    supports_streaming=True, 
                    caption=clean_caption
                )
            else:
                await message.answer_photo(
                    FSInputFile(p), 
                    caption=clean_caption
                )
        else:
            # Для альбома (карусели)
            album = MediaGroupBuilder(caption=clean_caption)
            for path in processed_paths[:10]:
                if path.lower().endswith(('.mp4', '.mov')):
                    w, h = get_video_dimensions(path)
                    album.add_video(media=FSInputFile(path), width=w, height=h)
                else:
                    album.add_photo(media=FSInputFile(path))
            await message.answer_media_group(album.build())

        await status_msg.delete()

    except Exception as e:
        logger.error(f"Error: {e}")
        await status_msg.edit_text(f"❌ Ошибка: {str(e)[:50]}")
    finally:
        asyncio.create_task(delayed_cleanup(temp_dir, 60))

async def delayed_cleanup(directory, delay):
    await asyncio.sleep(delay)
    if os.path.exists(directory): shutil.rmtree(directory)

async def main():
    if not os.path.exists(TEMP_BASE_DIR): os.makedirs(TEMP_BASE_DIR)
    logger.info("Бот запущен.")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())