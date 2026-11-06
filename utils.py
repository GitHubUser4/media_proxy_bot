import os
import json
import math
import asyncio
import logging
import requests
import subprocess
from config import MAX_SIZE_BYTES

logger = logging.getLogger(__name__)

def get_media_meta(file_path):
    cmd = [
        "ffprobe", "-v", "quiet", "-print_format", "json",
        "-show_streams", file_path,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        data = json.loads(result.stdout)
        for s in data.get("streams", []):
            if s.get("codec_type") == "video":
                return int(s.get("width")), int(s.get("height"))
    except Exception:
        pass
    return None, None

async def process_video(input_p):
    out_p = input_p.rsplit(".", 1)[0] + "_fixed.mp4"
    cmd = [
        "ffmpeg", "-y", "-i", input_p,
        "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
        "-vcodec", "libx264", "-pix_fmt", "yuv420p",
        "-profile:v", "main", "-crf", "26",
        "-preset", "veryfast",
        "-acodec", "aac", "-b:a", "128k",
        "-movflags", "+faststart", out_p,
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        _, stderr = await proc.communicate()

        if proc.returncode == 0 and os.path.exists(out_p):
            os.remove(input_p)
            return out_p
        else:
            logger.error(f"FFmpeg error: {stderr.decode()}")
    except Exception as e:
        logger.error(f"FFmpeg execution failed: {e}")
    return input_p

def split_large_video(video_path, output_dir, max_parts=3):
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
        subprocess.run(
            ffmpeg_cmd, shell=True, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, check=True,
        )

        generated_files = sorted([
            os.path.join(output_dir, f) for f in os.listdir(output_dir)
            if f.startswith("part_") and f.endswith(".mp4")
        ])
        if generated_files:
            return generated_files
    except Exception as e:
        logger.error(f"Ошибка при нарезке видео: {e}")

    return [video_path]

async def safe_download(url, path):
    try:
        r = requests.get(url, timeout=20)
        if r.status_code == 200:
            with open(path, "wb") as f:
                f.write(r.content)
            return True
    except Exception:
        pass
    return False