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
def get_video_dimensions(file_path):
    """Возвращает (width, height) видеофайла"""
    cmd = [
        'ffprobe', '-v', 'quiet', '-print_format', 'json', 
        '-show_streams', file_path
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        data = json.loads(result.stdout)
        for stream in data.get('streams', []):
            if stream.get('codec_type') == 'video':
                return int(stream.get('width')), int(stream.get('height'))
    except Exception as e:
        logger.error(f"Ошибка ffprobe: {e}")
    return None, None

def fix_video_for_telegram(input_path, output_path):
    """Исправляет пропорции видео и удаляет метаданные для Telegram"""
    command = [
        'ffmpeg', '-y', '-i', input_path,
        '-vf', 'scale=trunc(iw/2)*2:trunc(ih/2)*2,setsar=1', 
        '-vcodec', 'libx264', 
        '-crf', '30', 
        '-preset', 'superfast', 
        '-pix_fmt', 'yuv420p',
        '-map_metadata', '-1', 
        '-acodec', 'aac', '-b:a', '96k',
        '-movflags', '+faststart',
        output_path
    ]
    
    try:
        logger.info(f"🛠 FFmpeg: Начинаю обработку {input_path}")
        # Оставляем только один запуск!
        subprocess.run(command, check=True, capture_output=True, text=True)
        logger.info(f"✅ FFmpeg завершен: {output_path}")
        return output_path
    except Exception as e:
        logger.error(f"❌ Ошибка в FFmpeg: {e}")
        return input_path # Если не вышло, вернем оригинал
    
def clean_url(raw_url):
    """Удаляет мусорные параметры из ссылки"""
    parsed = urlparse(raw_url)
    return urlunparse(parsed._replace(query=""))

def get_dict_cookies():
    """Безопасное извлечение кук: сначала из браузера, если нет — из файла или пусто"""
    try:
        ydl_opts = {'cookiesfrombrowser': ('chrome',), 'quiet': True}
        with YoutubeDL(ydl_opts) as ydl:
            raw_cookies = ydl.cookiejar
        return [{'name': c.name, 'value': c.value, 'domain': c.domain, 'path': c.path, 'secure': True} for c in raw_cookies]
    except Exception as e:
        logger.warning(f"Браузер Chrome не найден ({e}), использую только файл кук.")
        return [] # Возвращаем пустоту, Playwright подхватит куки позже, если нужно

# --- 4. ЛОГИКА ЗАГРУЗКИ ---

def download_content(url, temp_path):
    ydl_opts = {
        'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
        'outtmpl': os.path.join(temp_path, 'media_%(id)s.%(ext)s'),
        'cookiefile': COOKIE_FILE if os.path.exists(COOKIE_FILE) else None,
        # 'cookiesfrombrowser': ('chrome',),  <-- УДАЛИ ЭТУ СТРОКУ
        'quiet': True,
        'no_warnings': True,
        'merge_output_format': 'mp4',
    }

    with YoutubeDL(ydl_opts) as ydl:
        # download=True дает команду yt-dlp сразу сохранить файл на диск
        info = ydl.extract_info(url, download=True)
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
# --- УЛУЧШЕННЫЙ ЛОВЕЦ ВИДЕО ---
            caught_video_url = None
            async def handle_response(response):
                nonlocal caught_video_url
                # Проверяем, что это медиа или типичный для инсты поток
                if response.request.resource_type in ["media", "fetch", "xhr"]:
                    content_type = response.headers.get("content-type", "").lower()
                    
                    if "video/mp4" in content_type or ".mp4" in response.url:
                        # ПРОВЕРКА НА РАЗМЕР: Пропускаем сегменты инициализации (обычно < 2000 байт)
                        try:
                            size = int(response.headers.get("content-length", 0))
                            if size > 50000:  # Берем только то, что больше 50 КБ
                                caught_video_url = response.url
                                logger.info(f"✅ Поймано реальное видео: {size} байт")
                        except:
                            # Если размера нет в заголовках, просто не берем первый попавшийся
                            if not caught_video_url and "bytestart" not in response.url:
                                caught_video_url = response.url

            context.on("response", handle_response)
            page = await context.new_page()

            try:
                # 1. Загружаем структуру страницы
                await page.goto(url, wait_until="domcontentloaded", timeout=60000)
                # 2. Ждем, когда сетевая активность утихнет (все картинки/скрипты догрузятся)
                try:
                    await page.wait_for_load_state("networkidle", timeout=10000)
                    await page.wait_for_selector("video, article img", timeout=5000)
                    # Даем немного времени на прогрузку скриптов
                    await asyncio.sleep(5) 
                    # Определяем путь в корне проекта, а не в temp
                    debug_screenshot_path = os.path.join(os.getcwd(), "last_error.png")

                    # ... после page.goto(url) и ожидания ...
                    await page.screenshot(path=debug_screenshot_path, full_page=True)
                    logger.info(f"📸 Скриншот для отладки обновлен: {debug_screenshot_path}")
                            except:
                    logger.warning("Контент не появился по селектору или сеть не затихла, пробуем так.")

                # 1. ПРОВЕРКА НА ВИДЕО
                video_element = await page.query_selector("video")
                if video_element or caught_video_url:
                    logger.info("Playwright: Видео в поле зрения. Ждем поток данных...")
                    # Ждем чуть дольше и проверяем, поймали ли мы что-то весомое
                    for _ in range(10): 
                        if caught_video_url: break
                        await asyncio.sleep(1) # Ждем до 10 секунд появления живой ссылки
                    
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
                        await next_btn.click(force=True)
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
    # Ищем ссылку внутри текста с помощью регулярного выражения
    url_match = re.search(r'(https?://[^\s]+)', message.text)
    if not url_match:
        return
    
    raw_url = url_match.group(1)
    url = clean_url(raw_url)
    
    status_msg = await message.answer("🚀 Танк поехал за медиа...")
    
    # Создаем временную папку ВНУТРИ папки downloads
    folder_name = f"temp_{message.from_user.id}_{int(time.time())}"
    temp_dir = os.path.join(TEMP_BASE_DIR, folder_name)
    os.makedirs(temp_dir, exist_ok=True)

    try:
# План А: Пытаемся скачать через yt-dlp (с 2 попытками)
        success = False
        for attempt in range(1, 3):
            try:
                await status_msg.edit_text(f"🔍 Пробую быстрый метод... (Попытка {attempt})")
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, download_content, url, temp_dir)
                success = True
                break # Если скачалось — выходим из цикла попыток
            except Exception as e:
                logger.warning(f"Попытка {attempt} не удалась: {e}")
                if attempt == 1: await asyncio.sleep(2) # Пауза перед второй попыткой

            if not success:
                # Только если План А провалился дважды — идем в План Б
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
            
            if file_name.lower().endswith(('.mp4', '.mov', '.m4v')):
                logger.info(f"🎯 Найдено видео: {file_name}. Запускаю фикс...")
                
                # Создаем имя для обработанного файла
                output_path = os.path.join(temp_dir, f"fixed_{file_name}")
                
                # ПЕРЕДАЕМ ОБА АРГУМЕНТА (input_path и output_path)
                fixed_path = await loop.run_in_executor(
                    None, 
                    fix_video_for_telegram, 
                    input_path, 
                    output_path
                )
                processed_paths.append(fixed_path)
            else:
                logger.info(f"📷 Это не видео, пропускаю фикс: {file_name}")
                processed_paths.append(input_path)

        # --- Отправка результата ---
        await status_msg.edit_text("📤 Отправляю результат...")

        if len(processed_paths) == 1:
            file_path = processed_paths[0]
            if file_path.lower().endswith('.mp4'):
                w, h = get_video_dimensions(file_path) # Узнаем размеры
                await message.answer_video(
                    FSInputFile(file_path),
                    width=w, height=h, # ПРЯМО ГОВОРИМ ТЕЛЕГРАМУ РАЗМЕРЫ
                    supports_streaming=True,
                    caption="Готово! 🎥"
                )
            else:
                await message.answer_photo(
                    FSInputFile(file_path), 
                    caption="Готово! 📸"
                )
        else:
            # Если файлов несколько (альбом)
            album = MediaGroupBuilder(caption="Твоя подборка готова!")
            for path in processed_paths[:10]:
                if path.lower().endswith('.mp4'):
                    w, h = get_video_dimensions(path)
                    album.add_video(media=FSInputFile(path), width=w, height=h)
                else:
                    album.add_photo(media=FSInputFile(path))
            
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