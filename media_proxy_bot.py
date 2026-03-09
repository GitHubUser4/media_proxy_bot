import os
import asyncio
import logging
import random
import shutil
import requests
import time
import subprocess
from urllib.parse import urlparse, urlunparse
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, Router, types, F
from aiogram.types import FSInputFile, InputMediaPhoto, Message
from aiogram.filters import Command
from aiogram.utils.media_group import MediaGroupBuilder
from playwright.async_api import async_playwright
from yt_dlp import YoutubeDL

# --- 1. ЗАГРУЗКА КОНФИГУРАЦИИ ---
load_dotenv()
api_key = os.getenv("API_TOKEN")
COOKIE_FILE = "instagram_cookies.txt"
TEMP_BASE_DIR = "downloads"
LOG_FILE = "proxy_bot.log"
MAX_SIZE_MB = 48  # Лимит Telegram для ботов

# Глобальные переменные для Playwright
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
def fix_video_for_telegram(input_path):
    output_path = input_path.replace(".mp4", "_processed.mp4")
    
    # Если расширение было не mp4, заменим его принудительно для вывода
    if not output_path.endswith("_processed.mp4"):
        output_path = os.path.splitext(input_path)[0] + "_processed.mp4"

    command = [
        'ffmpeg', '-y', '-i', input_path,
        '-vcodec', 'libx264', 
        '-crf', '28', 
        '-preset', 'superfast', 
        '-pix_fmt', 'yuv420p',
        '-acodec', 'aac', '-b:a', '128k',
        '-movflags', '+faststart',
        output_path
    ]
    
    try:
        logger.info(f"🛠 FFmpeg: Начинаю перекодирование {input_path} -> {output_path}")
        result = subprocess.run(command, check=True, capture_output=True, text=True)
        logger.info(f"✅ FFmpeg завершен успешно!")
        return output_path
    except Exception as e:
        logger.error(f"❌ Ошибка в fix_video_for_telegram: {e}")
        return input_path
    
def clean_url(raw_url):
    """Удаляет мусорные параметры из ссылки"""
    parsed = urlparse(raw_url)
    return urlunparse(parsed._replace(query=""))

def get_dict_cookies():
    """Синхронная функция для извлечения кук для Playwright"""
    ydl_opts = {'cookiesfrombrowser': ('chrome',), 'quiet': True}
    with YoutubeDL(ydl_opts) as ydl:
        raw_cookies = ydl.cookiejar
    return [{'name': c.name, 'value': c.value, 'domain': c.domain, 'path': c.path, 'secure': True} for c in raw_cookies]

# --- 4. ЛОГИКА ЗАГРУЗКИ ---

def download_content(url, temp_path):
    """План А: Быстрая загрузка через yt-dlp с приоритетом видео"""
    ydl_opts = {
        'cookiefile': COOKIE_FILE if os.path.exists(COOKIE_FILE) else None,
        'cookiesfrombrowser': ('chrome',),
        'quiet': True,
        'no_warnings': True,
        'ignore_no_formats_error': True,
        'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    }

    with YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
        targets = info['entries'] if 'entries' in info else [info]

        for index, item in enumerate(targets):
            media_url = None
            is_video = False

            # 1. Жесткий приоритет видео-форматам
            if item.get('formats'):
                videos = [f for f in item['formats'] if f.get('vcodec') != 'none' and f.get('url')]
                if videos:
                    media_url = videos[-1].get('url')
                    is_video = True
            
            # 2. Если не видео, берем url или display_resources
            if not media_url:
                media_url = item.get('url')
                is_video = item.get('ext') == 'mp4' or item.get('vcodec') not in ['none', None]

            if not media_url and item.get('display_resources'):
                media_url = item['display_resources'][-1].get('src')
                is_video = False
                
            if not media_url:
                media_url = item.get('thumbnail')
                is_video = False

            if media_url:
                # Финальная проверка по ссылке
                if '.mp4' in media_url:
                    is_video = True
                    
                ext = 'mp4' if is_video else 'jpg'
                file_path = os.path.join(temp_path, f"media_{index}.{ext}")
                
                headers = {'User-Agent': ydl_opts['user_agent'], 'Referer': 'https://www.instagram.com/'}
                resp = requests.get(media_url, headers=headers, stream=True, timeout=20)
                
                if resp.status_code == 200:
                    with open(file_path, 'wb') as f:
                        for chunk in resp.iter_content(8192): f.write(chunk)
                else:
                    logger.error(f"Ошибка загрузки {ext}: статус {resp.status_code}")

        if not os.listdir(temp_path):
            raise Exception("yt-dlp: Медиафайлы не найдены.")
        return info

async def download_insta_media_playwright(url, temp_dir):
    """План Б: Рендеринг каруселей ИЛИ перехват видео через браузер"""
    global cached_cookies
    async with browser_semaphore:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(viewport={'width': 1280, 'height': 1440})
            
            try:
                if not cached_cookies:
                    cached_cookies = await asyncio.to_thread(get_dict_cookies)
                await context.add_cookies(cached_cookies)
            except Exception as e:
                logger.error(f"Ошибка получения кук: {e}")

            # --- ЛОВЕЦ ВИДЕО (Network Interception) ---
            caught_video_url = None
            async def handle_response(response):
                nonlocal caught_video_url
                if response.request.resource_type in ["media", "fetch", "xhr"]:
                    if "video/mp4" in response.headers.get("content-type", "") or ".mp4" in response.url:
                        if "vp/" in response.url or "scontent" in response.url:
                            if not caught_video_url:
                                caught_video_url = response.url

            context.on("response", handle_response)
            page = await context.new_page()

            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=60000)
                await asyncio.sleep(5) 

                # 1. ПРОВЕРКА НА ВИДЕО
                video_element = await page.query_selector("video")
                if video_element or caught_video_url:
                    logger.info("Playwright: Это видео! Перехватываю mp4...")
                    # Даем сети пару секунд, чтобы точно поймать прямую ссылку
                    for _ in range(6):
                        if caught_video_url: break
                        await asyncio.sleep(0.5)
                    
                    video_src = caught_video_url or await video_element.get_attribute("src")
                    
                    if video_src and video_src.startswith("http"):
                        path = os.path.join(temp_dir, "video_playwright.mp4")
                        resp = await context.request.get(video_src)
                        with open(path, "wb") as f:
                            f.write(await resp.body())
                        return [path]
                    else:
                        logger.error("Playwright: Видео найдено, но ссылку извлечь не удалось.")
                        return []

                # 2. ЕСЛИ НЕ ВИДЕО - СОБИРАЕМ КАРУСЕЛЬ (ФОТО)
                await page.evaluate("""() => {
                    const trash = ['div[role="navigation"]', 'nav', 'aside', 'div._as9z', 'div[role="dialog"]'];
                    trash.forEach(s => {
                        document.querySelectorAll(s).forEach(el => el.style.display = 'none');
                    });
                }""")

                last_url, slide_count = "", 0
                saved_files = []

                while slide_count < 10:
                    await asyncio.sleep(2)
                    images = await page.query_selector_all("article img, ._aagv img, img")
                    target_img, current_url, max_area = None, "", 0
                    
                    for img in images:
                        if await img.is_visible():
                            box = await img.bounding_box()
                            if box and box['width'] > 300:
                                area = box['width'] * box['height']
                                if area > max_area:
                                    max_area, target_img = area, img
                                    current_url = await img.get_attribute("src")

                    if target_img and current_url != last_url:
                        slide_count += 1
                        last_url = current_url
                        path = os.path.join(temp_dir, f"slide_{slide_count}.jpg")
                        await target_img.screenshot(path=path)
                        saved_files.append(path)
                    
                    next_btn = await page.query_selector("button[aria-label='Далее'], button[aria-label='Next']")
                    if next_btn and await next_btn.is_visible():
                        await next_btn.click()
                    else:
                        break
                
                return saved_files

            except Exception as e:
                logger.error(f"Playwright: Ошибка рендеринга: {e}")
            finally:
                await browser.close()

# --- 5. ФОНОВАЯ ЗАДАЧА (KEEP-ALIVE) ---

async def session_keep_alive():
    """Поддержание сессии живой через Playwright (имитация активности)"""
    global cached_cookies, last_cookie_update
    while True:
        try:
            current_time = asyncio.get_event_loop().time()
            if not cached_cookies or (current_time - last_cookie_update > 7200):
                logger.info("🍪 Обновляю базу кук из Chrome...")
                cached_cookies = await asyncio.to_thread(get_dict_cookies)
                last_cookie_update = current_time

            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                context = await browser.new_context()
                if cached_cookies: await context.add_cookies(cached_cookies)
                
                page = await context.new_page()
                targets = ["https://www.instagram.com/", "https://www.instagram.com/explore/"]
                target = random.choice(targets)
                
                logger.info(f"🎭 Имитирую активность: {target}")
                await page.goto(target, wait_until="domcontentloaded")
                await asyncio.sleep(random.uniform(3, 7))
                await browser.close()

        except Exception as e:
            logger.error(f"⚠️ Ошибка в поддержании сессии: {e}")

        wait_time = random.randint(14400, 28800)
        logger.info(f"Плановое подтверждение сессии через {wait_time // 3600}ч.")
        await asyncio.sleep(wait_time)

# --- 6. ОБРАБОТЧИК СООБЩЕНИЙ ---

@dp.message(F.text.contains("instagram.com"))

async def handle_instagram(message: types.Message):
    """Основной обработчик ссылок Instagram"""
    # Очищаем ссылку от мусора перед работой!
    raw_url = message.text.strip()
    url = clean_url(raw_url)
    
    status_msg = await message.answer("🚀 Танк поехал за медиа...")
    
    # Создаем временную папку ВНУТРИ папки downloads
    folder_name = f"temp_{message.from_user.id}_{int(time.time())}"
    temp_dir = os.path.join(TEMP_BASE_DIR, folder_name)
    os.makedirs(temp_dir, exist_ok=True)

    try:
        # План А: Пытаемся скачать через yt-dlp
        try:
            await status_msg.edit_text("🔍 Пробую быстрый метод (yt-dlp)...")
            # Мы используем run_in_executor, чтобы не блокировать бота во время скачивания
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, download_content, url, temp_dir)
        except Exception as e:
            logger.warning(f"План А не сработал: {e}. Перехожу к Плану Б...")
            # План Б: Playwright (браузер)
            await status_msg.edit_text("🌐 План А мимо. Запускаю браузер (Playwright)...")
            files_from_pw = await download_insta_media_playwright(url, temp_dir)
            if not files_from_pw:
                raise Exception("Ни один метод не смог достать медиа.")

       # --- Проверяем, что скачалось ---
        downloaded_files = os.listdir(temp_dir)
        logger.info(f"Файлы в папке {temp_dir}: {downloaded_files}") # Посмотрим, что там лежит

        if not downloaded_files:
            raise Exception("Файлы не найдены в папке после загрузки")

        await status_msg.edit_text("⚙️ Обработка и сжатие...")

        processed_paths = []
        loop = asyncio.get_event_loop()
        
        for file_name in downloaded_files:
            input_path = os.path.join(temp_dir, file_name)
            
            # Проверяем расширение (игнорируя регистр)
            if file_name.lower().endswith(('.mp4', '.mov', '.m4v')):
                logger.info(f"🎯 Найдено видео: {file_name}. Запускаю фикс...")
                # Запускаем нашу «золотую» функцию из старого бота
                fixed_path = await loop.run_in_executor(None, fix_video_for_telegram, input_path)
                processed_paths.append(fixed_path)
            else:
                logger.info(f"📷 Это не видео, пропускаю фикс: {file_name}")
                processed_paths.append(input_path)

        # --- Отправка результата ---
        await status_msg.edit_text("📤 Отправляю результат...")

        if len(processed_paths) == 1:
            file_path = processed_paths[0]
            if file_path.lower().endswith('.mp4'):
                await message.answer_video(
                    FSInputFile(file_path), 
                    caption="Готово! 🎥"
                )
            else:
                await message.answer_photo(
                    FSInputFile(file_path), 
                    caption="Готово! 📸"
                )
        else:
            # Если файлов несколько, собираем альбом (Media Group)
            album = MediaGroupBuilder(caption="Твоя подборка готова!")
            for path in processed_paths[:10]: # Ограничение Telegram на 10 медиа в группе
                if path.lower().endswith('.mp4'):
                    album.add_video(FSInputFile(path))
                else:
                    album.add_photo(FSInputFile(path))
            
            await message.answer_media_group(album.build())

        await status_msg.delete()

    except Exception as e:
        logger.error(f"Ошибка в handle_instagram: {e}")
        await status_msg.edit_text(f"❌ Не удалось загрузить: {str(e)[:100]}")
    
    finally:
        # Очистка: удаляем временную папку и все файлы в ней через пару минут
        # или сразу (лучше через небольшую паузу, чтобы Telegram успел отправить)
        asyncio.create_task(delayed_cleanup(temp_dir, delay=60))

async def delayed_cleanup(directory, delay):
    """Удаляет временные файлы после паузы"""
    await asyncio.sleep(delay)
    if os.path.exists(directory):
        shutil.rmtree(directory)
        logger.info(f"Очищена папка: {directory}")

# --- 7. ЗАПУСК ---

async def main():
    if not os.path.exists(TEMP_BASE_DIR):
        os.makedirs(TEMP_BASE_DIR)
        
    # Запускаем прогрев сессии
    asyncio.create_task(session_keep_alive())
    
    logger.info("Бот запущен и готов к работе.")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Бот остановлен пользователем.")