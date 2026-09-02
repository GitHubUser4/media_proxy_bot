import os, asyncio, logging, shutil, requests, time, subprocess, json, re, psutil, platform, math
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
IG_COOKIE_FILE = "instagram_cookies.txt"
YT_COOKIE_FILE = "youtube_cookies.txt"
TEMP_BASE_DIR = "downloads"

# Динамические лимиты из .env (с дефолтными значениями)
MAX_SIZE_BYTES = int(os.getenv("MAX_VIDEO_SIZE_MB", 50)) * 1024 * 1024 
MAX_YT_DURATION_SEC = int(os.getenv("MAX_YT_DURATION_SEC", 1500))
MAX_VIDEO_PARTS = 3 # Максимальное количество кусков, на которые режем одно видео

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
    """Оптимизация видео для гарантированной кроссплатформенности."""
    cmd = [
        'ffmpeg', '-y', '-i', input_p,
        '-vf', 'scale=trunc(iw/2)*2:trunc(ih/2)*2', 
        '-vcodec', 'libx264', '-pix_fmt', 'yuv420p', '-crf', '26', '-preset', 'superfast', 
        '-acodec', 'aac', '-b:a', '128k', '-movflags', '+faststart', output_p
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=60)
        return output_p
    except Exception as e:
        logger.error(f"FFmpeg error: {e}")
        return input_p

def split_large_video(video_path, output_dir, max_parts=3):
    """Нарезка тяжелого видео на части по ключевым кадрам без потери качества."""
    file_size_mb = os.path.getsize(video_path) / (1024 * 1024)
    
    if file_size_mb <= (MAX_SIZE_BYTES / (1024 * 1024)):
        return [video_path]
        
    parts_needed = math.ceil(file_size_mb / 45)
    
    if parts_needed > max_parts:
        logger.warning(f"Видео слишком тяжелое: {file_size_mb:.2f} MB требует {parts_needed} частей.")
        return None
        
    try:
        cmd_duration = f'ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 "{video_path}"'
        duration = float(subprocess.check_output(cmd_duration, shell=True).decode().strip())
        
        segment_time = math.ceil(duration / parts_needed)
        output_pattern = os.path.join(output_dir, "part_%03d.mp4")
        
        ffmpeg_cmd = (
            f'ffmpeg -y -i "{video_path}" -c copy -f segment '
            f'-segment_time {segment_time} -reset_timestamps 1 "{output_pattern}"'
        )
        
        subprocess.run(ffmpeg_cmd, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        
        generated_files = sorted([
            os.path.join(output_dir, f) for f in os.listdir(output_dir) if f.startswith("part_") and f.endswith(".mp4")
        ])
        
        if generated_files:
            return generated_files
    except Exception as e:
        logger.error(f"Ошибка при нарезке видео: {e}")
        
    return [video_path]

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
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox"]
            )
            context = await browser.new_context(viewport={'width': 1280, 'height': 1440})
            
            if os.path.exists(IG_COOKIE_FILE):
                try:
                    ydl = YoutubeDL({'cookiefile': IG_COOKIE_FILE, 'quiet': True})
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

                caption = await page.evaluate("() => document.querySelector('article h1')?.innerText || ''")
                
                all_urls = set()
                for _ in range(12):
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

@dp.message(Command("mp3"))
async def handle_youtube_mp3(message: Message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("⚠️ Отправь ссылку: `/mp3 https://youtu.be/...`", parse_mode="Markdown")
        return

    url = parts[1]
    if "youtube.com" not in url and "youtu.be" not in url:
        return

    status_msg = await message.answer("🔍 Проверяю размер аудио...")

    try:
        def get_video_info():
            opts = {'quiet': True, 'cookiefile': YT_COOKIE_FILE if os.path.exists(YT_COOKIE_FILE) else None}
            with YoutubeDL(opts) as ydl:
                return ydl.extract_info(url, download=False)

        loop = asyncio.get_event_loop()
        info = await loop.run_in_executor(None, get_video_info)

        duration = info.get('duration', 0)
        if duration > MAX_YT_DURATION_SEC:
            await status_msg.edit_text(
                f"🛑 Видео идет {duration // 60} мин. Лимит конфига — {MAX_YT_DURATION_SEC // 60} мин. "
                f"Бот не станет это качать."
            )
            return

        await status_msg.edit_text("🎵 Скачиваю и конвертирую в MP3...")
        t_dir = os.path.join(TEMP_BASE_DIR, f"yt_{message.from_user.id}_{int(time.time())}")
        os.makedirs(t_dir, exist_ok=True)

        def download_audio():
            ydl_opts = {
                'format': 'bestaudio/best',
                'outtmpl': os.path.join(t_dir, '%(title)s.%(ext)s'),
                'writethumbnail': True,
				'extractor_args': {'youtube': {'player_client': ['android', 'web']}},
                'postprocessors': [
                    {'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': '320'},
                    {'key': 'FFmpegThumbnailsConvertor', 'format': 'jpg'}
                ],
                'quiet': True,
                'cookiefile': YT_COOKIE_FILE if os.path.exists(YT_COOKIE_FILE) else None
            }
            with YoutubeDL(ydl_opts) as ydl:
                ydl.extract_info(url, download=True)

        await loop.run_in_executor(None, download_audio)

        mp3_files = [f for f in os.listdir(t_dir) if f.endswith('.mp3')]
        if not mp3_files:
            raise Exception("Не удалось конвертировать аудио.")
        mp3_path = os.path.join(t_dir, mp3_files[0])

        thumb_files = [f for f in os.listdir(t_dir) if f.endswith('.jpg')]
        thumb_path = os.path.join(t_dir, thumb_files[0]) if thumb_files else None
        thumb_input = FSInputFile(thumb_path) if thumb_path else None

        await status_msg.edit_text("📤 Отправляю трек...")
        
        await message.answer_audio(
            FSInputFile(mp3_path),
            caption="🎧 Аудио с YouTube",
            title=info.get('title', 'Unknown Title'),
            performer=info.get('uploader', 'YouTube'),
            thumbnail=thumb_input
        )
        await status_msg.delete()

    except Exception as e:
        logger.error(f"Ошибка YT MP3: {e}")
        await status_msg.edit_text(f"❌ Ошибка: {str(e)[:50]}")
    finally:
        if 't_dir' in locals() and os.path.exists(t_dir):
            async def cleanup():
                await asyncio.sleep(60)
                shutil.rmtree(t_dir, ignore_errors=True)
            asyncio.create_task(cleanup())

@dp.message(Command("status"))
async def cmd_status(msg: Message):
    if msg.from_user.id != ADMIN_ID: return
    ram, disk = psutil.virtual_memory(), psutil.disk_usage('/')
    ig_time = datetime.fromtimestamp(os.path.getmtime(IG_COOKIE_FILE)).strftime('%d.%m %H:%M') if os.path.exists(IG_COOKIE_FILE) else "None"
    yt_time = datetime.fromtimestamp(os.path.getmtime(YT_COOKIE_FILE)).strftime('%d.%m %H:%M') if os.path.exists(YT_COOKIE_FILE) else "None"
    await msg.answer(f"<b>📊 Статус:</b>\nRAM: {ram.percent}%\nDisk free: {disk.free // 10**9}GB\nInsta_cookies: {ig_time}\nYoutube_cookies: {yt_time}", parse_mode="HTML")

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
        await status.edit_text("🔍 Метод A...")
        try:
            loop = asyncio.get_event_loop()
            info = await loop.run_in_executor(None, lambda: YoutubeDL({'outtmpl': f'{t_dir}/%(id)s.%(ext)s', 'quiet': True, 'cookiefile': IG_COOKIE_FILE}).extract_info(clean_url, download=True))
            files = [os.path.join(t_dir, f) for f in os.listdir(t_dir)]
            caption = info.get('description') or info.get('title') or ""
        except: pass

        if not files:
            await status.edit_text("🌐 Метод Б...")
            files, caption = await fetch_with_playwright(clean_url, t_dir)

        if not files: raise Exception("Контент не найден.")

        await status.edit_text("⚙️ Финализация и нарезка...")
        final_media = []
        for f in files:
            # Если это видео — проверяем вес и при необходимости режем
            if f.lower().endswith(('.mp4', '.mov')):
                parts = split_large_video(f, t_dir, MAX_VIDEO_PARTS)
                if parts:
                    final_media.extend(parts)
                else:
                    logger.warning(f"Файл {f} пропущен, так как не может быть порезан (превышен лимит кусков).")
            # Если это картинка — просто проверяем, чтобы не весила больше 50 МБ
            else:
                if os.path.getsize(f) <= MAX_SIZE_BYTES:
                    final_media.append(f)

        if not final_media: raise Exception("Файлы слишком тяжелые для TG.")

        warning_text = "🔞 Материал 18+\n\n"
        max_text_length = 1024 - len(warning_text)
        original_text = (caption or "")[:max_text_length]
        final_caption = warning_text + original_text

        chunks = [final_media[i:i + 10] for i in range(0, len(final_media), 10)]

        for index, chunk in enumerate(chunks):
            current_caption = final_caption if index == 0 else ""
            
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
                            for i, f in enumerate(chunk):
                                if f.endswith('.mp4'):
                                    w, h = get_media_meta(f)
                                    # Если файл был порезан, добавляем подпись "Часть X"
                                    title = f"Часть {i+1}" if "part_" in f else None
                                    alb.add_video(media=FSInputFile(f), width=w, height=h, title=title)
                                else:
                                    alb.add_photo(media=FSInputFile(f))
                            await msg.answer_media_group(alb.build())
                        return True 
                    except TelegramRetryAfter as e:
                        logger.warning(f"Flood limit! Спим {e.retry_after} сек.")
                        await asyncio.sleep(e.retry_after + 1)
                    except Exception as e:
                        logger.error(f"Ошибка отправки чанка: {e}")
                        break
                return False

            success = await send_with_retry()
            
            if success and len(chunks) > 1 and index < len(chunks) - 1:
                await asyncio.sleep(7) 

        await status.delete()
    except Exception as e:
        await status.edit_text(f"❌ {str(e)[:50]}")
    finally:
        async def cleanup(): 
            await asyncio.sleep(120)
            shutil.rmtree(t_dir, ignore_errors=True)
        asyncio.create_task(cleanup())

async def main():
    os.makedirs(TEMP_BASE_DIR, exist_ok=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())