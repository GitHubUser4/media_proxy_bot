import os
import re
import time
import shutil
import psutil
import asyncio
import logging
import config
from datetime import datetime
from urllib.parse import urlparse, urlunparse

from aiogram import Router, F
from aiogram.types import FSInputFile, Message
from aiogram.filters import Command
from aiogram.utils.media_group import MediaGroupBuilder
from aiogram.exceptions import TelegramRetryAfter
from yt_dlp import YoutubeDL

from config import (
    ADMIN_ID,
    YT_COOKIE_FILE,
    IG_COOKIE_FILE,
    TEMP_BASE_DIR,
    MAX_YT_DURATION_SEC,
    MAX_SIZE_BYTES,
    MAX_VIDEO_PARTS,
)
from utils import get_media_meta, process_video, split_large_video
from downloaders import fetch_with_playwright

logger = logging.getLogger(__name__)
router = Router()


@router.message(Command("mp3"))
async def handle_youtube_mp3(message: Message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer(
            "⚠️ Отправь ссылку: `/mp3 https://youtu.be/...`", parse_mode="Markdown"
        )
        return

    url = parts[1]
    if "youtube.com" not in url and "youtu.be" not in url:
        return

    status_msg = await message.answer("🔍 Проверяю размер аудио...")

    try:

        def get_video_info():
            opts = {
                "quiet": True,
                "cookiefile": YT_COOKIE_FILE
                if os.path.exists(YT_COOKIE_FILE)
                else None,
            }
            with YoutubeDL(opts) as ydl:
                return ydl.extract_info(url, download=False)

        loop = asyncio.get_event_loop()
        info = await loop.run_in_executor(None, get_video_info)
        duration = info.get("duration", 0)

        if duration > MAX_YT_DURATION_SEC:
            await status_msg.edit_text(
                f"🛑 Видео идет {duration // 60} мин. Лимит конфига — {MAX_YT_DURATION_SEC // 60} мин. Бот не станет это качать."
            )
            return

        await status_msg.edit_text("🎵 Скачиваю и конвертирую в MP3...")
        t_dir = os.path.join(
            TEMP_BASE_DIR, f"yt_{message.from_user.id}_{int(time.time())}"
        )
        os.makedirs(t_dir, exist_ok=True)

        def download_audio():
            ydl_opts = {
                "format": "bestaudio/best",
                "outtmpl": os.path.join(t_dir, "%(title)s.%(ext)s"),
                "writethumbnail": True,
                "extractor_args": {"youtube": {"player_client": ["android", "web"]}},
                "postprocessors": [
                    {
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": "mp3",
                        "preferredquality": "320",
                    },
                    {"key": "FFmpegThumbnailsConvertor", "format": "jpg"},
                ],
                "quiet": True,
                "cookiefile": YT_COOKIE_FILE
                if os.path.exists(YT_COOKIE_FILE)
                else None,
            }
            with YoutubeDL(ydl_opts) as ydl:
                ydl.extract_info(url, download=True)

        await loop.run_in_executor(None, download_audio)

        mp3_files = [f for f in os.listdir(t_dir) if f.endswith(".mp3")]
        if not mp3_files:
            raise Exception("Не удалось конвертировать аудио.")

        mp3_path = os.path.join(t_dir, mp3_files[0])
        thumb_files = [f for f in os.listdir(t_dir) if f.endswith(".jpg")]
        thumb_path = os.path.join(t_dir, thumb_files[0]) if thumb_files else None
        thumb_input = FSInputFile(thumb_path) if thumb_path else None

        await status_msg.edit_text("📤 Отправляю трек...")
        await message.answer_audio(
            FSInputFile(mp3_path),
            caption="🎧 Аудио с YouTube",
            title=info.get("title", "Unknown Title"),
            performer=info.get("uploader", "YouTube"),
            thumbnail=thumb_input,
        )
        await status_msg.delete()

    except Exception as e:
        logger.error(f"Ошибка YT MP3: {e}")
        await status_msg.edit_text(f"❌ Ошибка: {str(e)[:50]}")
    finally:
        if "t_dir" in locals() and os.path.exists(t_dir):

            async def cleanup():
                await asyncio.sleep(60)
                shutil.rmtree(t_dir, ignore_errors=True)

            asyncio.create_task(cleanup())


@router.message(Command("status"))
async def cmd_status(msg: Message):
    if msg.from_user.id != ADMIN_ID:
        return
    ram, disk = psutil.virtual_memory(), psutil.disk_usage("/")
    ig_time = (
        datetime.fromtimestamp(os.path.getmtime(IG_COOKIE_FILE)).strftime("%d.%m %H:%M")
        if os.path.exists(IG_COOKIE_FILE)
        else "None"
    )
    yt_time = (
        datetime.fromtimestamp(os.path.getmtime(YT_COOKIE_FILE)).strftime("%d.%m %H:%M")
        if os.path.exists(YT_COOKIE_FILE)
        else "None"
    )

    await msg.answer(
        f"<b>📊 Статус:</b>\nRAM: {ram.percent}%\nDisk free: {disk.free // 10**9}GB\nInsta_cookies: {ig_time}\nYoutube_cookies: {yt_time}",
        parse_mode="HTML",
    )


@router.message(F.text.contains("instagram.com"))
async def handle_insta(msg: Message):
    url_match = re.search(r"(https?://[^\s]+)", msg.text)
    if not url_match:
        return

    clean_url = urlunparse(urlparse(url_match.group(1))._replace(query=""))
    status = await msg.answer("⌛ В очереди на обработку...")

    # Блокировка и выдерживание интервала
    async with config.insta_lock:
        now = time.time()
        elapsed = now - config.last_insta_request_time
        if elapsed < config.MIN_INSTA_INTERVAL:
            await asyncio.sleep(config.MIN_INSTA_INTERVAL - elapsed)
        config.last_insta_request_time = time.time()

        await status.edit_text("⌛ Работаю...")
        t_dir = os.path.join(TEMP_BASE_DIR, f"{msg.from_user.id}_{int(time.time())}")
        os.makedirs(t_dir, exist_ok=True)

        try:
            files, caption = [], ""
            await status.edit_text("🔍 Метод A...")

            try:
                loop = asyncio.get_event_loop()
                info = await loop.run_in_executor(
                    None,
                    lambda: YoutubeDL(
                        {
                            "outtmpl": f"{t_dir}/%(id)s.%(ext)s",
                            "quiet": True,
                            "cookiefile": IG_COOKIE_FILE,
                        }
                    ).extract_info(clean_url, download=True),
                )
                files = [os.path.join(t_dir, f) for f in os.listdir(t_dir)]
                caption = info.get("description") or info.get("title") or ""
            except Exception:
                pass

            if not files:
                await status.edit_text("🌐 Метод Б...")
                files, caption = await fetch_with_playwright(clean_url, t_dir)

            if not files:
                raise Exception("Контент не найден.")

            await status.edit_text("⚙️ Финализация и нарезка...")
            final_media = []

            for f in files:
                if f.lower().endswith((".mp4", ".mov")):
                    fixed_file = await process_video(f)
                    parts = split_large_video(fixed_file, t_dir, MAX_VIDEO_PARTS)
                    if parts:
                        final_media.extend(parts)
                    else:
                        logger.warning(
                            f"Файл {f} пропущен, так как не может быть порезан."
                        )
                else:
                    if os.path.getsize(f) <= MAX_SIZE_BYTES:
                        final_media.append(f)

            if not final_media:
                raise Exception("Файлы слишком тяжелые для TG.")

            warning_text = "🔞 Материал 18+\n\n"
            max_text_length = 1024 - len(warning_text)
            original_text = (caption or "")[:max_text_length]
            final_caption = warning_text + original_text

            chunks = [final_media[i : i + 10] for i in range(0, len(final_media), 10)]

            for index, chunk in enumerate(chunks):
                current_caption = final_caption if index == 0 else ""

                async def send_with_retry(attempts=3):
                    for _ in range(attempts):
                        try:
                            if len(chunk) == 1:
                                f = chunk[0]
                                if f.endswith(".mp4"):
                                    w, h = get_media_meta(f)
                                    await msg.answer_video(
                                        FSInputFile(f),
                                        caption=current_caption,
                                        width=w,
                                        height=h,
                                    )
                                else:
                                    await msg.answer_photo(
                                        FSInputFile(f), caption=current_caption
                                    )
                            else:
                                alb = MediaGroupBuilder(caption=current_caption)
                                for i, f in enumerate(chunk):
                                    if f.endswith(".mp4"):
                                        w, h = get_media_meta(f)
                                        title = f"Часть {i+1}" if "part_" in f else None
                                        alb.add_video(
                                            media=FSInputFile(f),
                                            width=w,
                                            height=h,
                                            title=title,
                                        )
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