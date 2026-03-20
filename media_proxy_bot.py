import os, asyncio, logging, shutil, requests, time, subprocess, json, re, psutil, platform
from urllib.parse import urlparse, urlunparse
from dotenv import load_dotenv
from datetime import datetime
from aiogram import Bot, Dispatcher, F, types
from aiogram.types import FSInputFile, Message
from aiogram.filters import Command
from aiogram.utils.media_group import MediaGroupBuilder
from aiogram.exceptions import TelegramRetryAfter
from playwright.async_api import async_playwright
from yt_dlp import YoutubeDL

# --- КОНФИГУРАЦИЯ ---
load_dotenv()
API_KEY = os.getenv("API_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", 0))
COOKIE_FILE = "instagram_cookies.txt"
TEMP_BASE_DIR = "downloads"
MAX_SIZE_BYTES = 50 * 1024 * 1024 

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

bot = Bot(token=API_KEY)
dp = Dispatcher()
browser_semaphore = asyncio.Semaphore(1)

# --- УТИЛИТЫ ---

def get_media_meta(file_path):
    """Получает размеры видео через ffprobe."""
    cmd = ['ffprobe', '-v', 'quiet', '-print_format', 'json', '-show_streams', file_path]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        data = json.loads(result.stdout)
        for s in data.get('streams', []):
            if s.get('codec_type') == 'video':
                return int(s.get('width')), int(s.get('height'))
    except: pass
    return None, None

def process_video(input_p, output_p):
    """Оптимизация видео для Telegram."""
    cmd = [
        'ffmpeg', '-y', '-i', input_p,
        '-vf', 'scale=trunc(iw/2)*2:trunc(ih/2)*2', 
        '-vcodec', 'libx264', '-crf', '28', '-preset', 'superfast', 
        '-acodec', 'aac', '-b:a', '96k', '-movflags', '+faststart', output_p
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=60)
        return output_p
    except: return input_p

async def safe_download(url, path):
    """Скачивание файла с таймаутом."""
    try:
        r = requests.get(url, timeout=20)
        if r.status_code == 200:
            with open(path, "wb") as f: f.write(r.content)
            return True
    except: pass
    return False

# --- ЛОГИКА ЗАГРУЗКИ ---

async def fetch_with_playwright(url, temp_dir):
    """План Б: Эмуляция браузера."""
    async with browser_semaphore:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(viewport={'width': 1280, 'height': 1440})
            
            # Впрыск кук через yt-dlp cookiejar (твой метод)
            if os.path.exists(COOKIE_FILE):
                try:
                    ydl = YoutubeDL({'cookiefile': COOKIE_FILE, 'quiet': True})
                    await context.add_cookies([{
                        'name': c.name, 'value': c.value, 'domain': c.domain, 'path': c.path, 'secure': True
                    } for c in ydl.cookiejar])
                except Exception as e: logger.error(f"Cookie error: {e}")

            page = await context.new_page()
            saved_files, caption = [], ""
            
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=60000)
                await asyncio.sleep(4)
                
                if "login" in page.url:
                    return [], "Нужны свежие куки (Login page detected)"

                # Извлечение текста
                caption = await page.evaluate("() => document.querySelector('article h1')?.innerText || ''")
                
                all_urls = set()
                for _ in range(12): # Instagram обычно не дает больше 10 слайдов
                    elements = await page.query_selector_all("video, img")
                    for el in elements:
                        box = await el.bounding_box()
                        if not box or box['width'] < 300 or box['y'] > 700: continue
                        
                        src = await el.get_attribute("src")
                        if src and src not in all_urls:
                            all_urls.add(src)
                            ext = "mp4" if await el.evaluate("n => n.tagName") == "VIDEO" else "jpg"
                            f_path = os.path.join(temp_dir, f"file_{len(all_urls)}.{ext}")
                            if await safe_download(src, f_path): saved_files.append(f_path)
                    
                    next_btn = await page.query_selector("button[aria-label*='Next'], button[aria-label*='Далее']")
                    if next_btn and await next_btn.is_visible(): await next_btn.click(); await asyncio.sleep(1.5)
                    else: break
            finally:
                await browser.close()
            return saved_files, caption

# --- ОБРАБОТЧИКИ ---

@dp.message(Command("status"))
async def cmd_status(msg: Message):
    if msg.from_user.id != ADMIN_ID: return
    ram, disk = psutil.virtual_memory(), psutil.disk_usage('/')
    c_time = datetime.fromtimestamp(os.path.getmtime(COOKIE_FILE)).strftime('%d.%m %H:%M') if os.path.exists(COOKIE_FILE) else "None"
    await msg.answer(f"<b>📊 Статус:</b>\nRAM: {ram.percent}%\nDisk free: {disk.free // 10**9}GB\nCookies: {c_time}", parse_mode="HTML")

@dp.message(F.text.contains("instagram.com"))
async def handle_insta(msg: Message):
    url_match = re.search(r'(https?://[^\s]+)', msg.text)
    if not url_match: return
    
    clean_url = urlunparse(urlparse(url_match.group(1))._replace(query=""))
    status = await msg.answer("⌛ Работаю...")
    t_dir = os.path.join(TEMP_BASE_DIR, f"{msg.from_user.id}_{int(time.time())}")
    os.makedirs(t_dir, exist_ok=True)

    try:
        files, caption = [], ""
        # План А
        await status.edit_text("🔍 Метод A...")
        try:
            loop = asyncio.get_event_loop()
            info = await loop.run_in_executor(None, lambda: YoutubeDL({'outtmpl': f'{t_dir}/%(id)s.%(ext)s', 'quiet': True, 'cookiefile': COOKIE_FILE}).extract_info(clean_url, download=True))
            files = [os.path.join(t_dir, f) for f in os.listdir(t_dir)]
            caption = info.get('description') or info.get('title') or ""
        except: pass

        # План Б
        if not files:
            await status.edit_text("🌐 Метод Б...")
            files, caption = await fetch_with_playwright(clean_url, t_dir)

        if not files: raise Exception("Контент не найден.")

        # Обработка и фильтрация размера
        await status.edit_text("⚙️ Финализация...")
        final_media = []
        for f in files:
            if os.path.getsize(f) > MAX_SIZE_BYTES: continue
            if f.lower().endswith(('.mp4', '.mov')):
                f = process_video(f, f"{f}_fixed.mp4")
            final_media.append(f)

        if not final_media: raise Exception("Файлы слишком тяжелые для TG.")

# --- ОТПРАВКА С ЗАЩИТОЙ ОТ FLOOD ---
        caption = (caption or "")[:1024]
        chunks = [final_media[i:i + 10] for i in range(0, len(final_media), 10)]

        for index, chunk in enumerate(chunks):
            current_caption = caption if index == 0 else ""
            
            # Внутренняя функция для повторной попытки при флуде
            async def send_with_retry(attempts=3):
                for attempt in range(attempts):
                    try:
                        if len(chunk) == 1:
                            f = chunk[0]
                            if f.endswith('.mp4'):
                                w, h = get_media_meta(f)
                                await msg.answer_video(FSInputFile(f), caption=current_caption, width=w, height=h)
                            else:
                                await msg.answer_photo(FSInputFile(f), caption=current_caption)
                        else:
                            alb = MediaGroupBuilder(caption=current_caption)
                            for f in chunk:
                                if f.endswith('.mp4'):
                                    w, h = get_media_meta(f)
                                    alb.add_video(media=FSInputFile(f), width=w, height=h)
                                else:
                                    alb.add_photo(media=FSInputFile(f))
                            await msg.answer_media_group(alb.build())
                        return True # Успешно отправлено
                    except TelegramRetryAfter as e:
                        logger.warning(f"Flood limit! Спим {e.retry_after} сек.")
                        await asyncio.sleep(e.retry_after + 1)
                    except Exception as e:
                        logger.error(f"Ошибка отправки чанка: {e}")
                        break
                return False

            # Пытаемся отправить чанк
            success = await send_with_retry()
            
            # Если чанков много, делаем паузу ПОБОЛЬШЕ между ними
            if success and len(chunks) > 1 and index < len(chunks) - 1:
                await asyncio.sleep(7) # 7 секунд — безопасный интервал для тяжелых паков

        await status.delete()
    except Exception as e:
        await status.edit_text(f"❌ {str(e)[:50]}")
    finally:
        # Очистка через 2 минуты
        async def cleanup(): await asyncio.sleep(120); shutil.rmtree(t_dir, ignore_errors=True)
        asyncio.create_task(cleanup())

async def main():
    os.makedirs(TEMP_BASE_DIR, exist_ok=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())