import os
import asyncio
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv("API_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", 0))

IG_COOKIE_FILE = "instagram_cookies.txt"
YT_COOKIE_FILE = "youtube_cookies.txt"
TEMP_BASE_DIR = "downloads"

# Лимиты
MAX_SIZE_BYTES = int(os.getenv("MAX_VIDEO_SIZE_MB", 50)) * 1024 * 1024
MAX_YT_DURATION_SEC = int(os.getenv("MAX_YT_DURATION_SEC", 1500))
MAX_VIDEO_PARTS = 3

MIN_INSTA_INTERVAL = 12
insta_lock = asyncio.Lock()
last_insta_request_time = 0.0