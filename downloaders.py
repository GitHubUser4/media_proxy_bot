import os
import asyncio
import logging
from playwright.async_api import async_playwright
from yt_dlp import YoutubeDL
from config import IG_COOKIE_FILE
from utils import safe_download

logger = logging.getLogger(__name__)
browser_semaphore = asyncio.Semaphore(1)

async def fetch_with_playwright(url, temp_dir):
    async with browser_semaphore:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True, args=["--no-sandbox", "--disable-setuid-sandbox"]
            )
            context = await browser.new_context(viewport={"width": 1280, "height": 1440})

            if os.path.exists(IG_COOKIE_FILE):
                try:
                    ydl = YoutubeDL({"cookiefile": IG_COOKIE_FILE, "quiet": True})
                    await context.add_cookies([
                        {
                            "name": c.name, "value": c.value, "domain": c.domain,
                            "path": c.path, "secure": True,
                        }
                        for c in ydl.cookiejar
                    ])
                except Exception as e:
                    logger.error(f"Cookie error: {e}")

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
                        if not box or box["width"] < 300 or box["y"] > 700:
                            continue

                        src = await el.get_attribute("src")
                        if src and src not in all_urls:
                            all_urls.add(src)
                            ext = "mp4" if await el.evaluate("n => n.tagName") == "VIDEO" else "jpg"
                            f_path = os.path.join(temp_dir, f"file_{len(all_urls)}.{ext}")
                            if await safe_download(src, f_path):
                                saved_files.append(f_path)

                    next_btn = await page.query_selector("button[aria-label*='Next'], button[aria-label*='Далее']")
                    if next_btn and await next_btn.is_visible():
                        await next_btn.click()
                        await asyncio.sleep(1.5)
                    else:
                        break
            finally:
                await browser.close()
            return saved_files, caption