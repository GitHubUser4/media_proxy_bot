import os
import asyncio
import logging
from playwright.async_api import async_playwright
from yt_dlp import YoutubeDL
from config import IG_COOKIE_FILE
import requests

logger = logging.getLogger(__name__)
browser_semaphore = asyncio.Semaphore(1)


def _download_sync(url, path):
    try:
        r = requests.get(url, timeout=20)
        if r.status_code == 200:
            with open(path, "wb") as f:
                f.write(r.content)
            return True
    except Exception:
        pass
    return False

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
                loop = asyncio.get_event_loop()

                for _ in range(12):
                    media = await page.evaluate(
                        "() => [...document.querySelectorAll('video, img')].map(n => {"
                        " const r = n.getBoundingClientRect();"
                        " return {tag: n.tagName, src: n.currentSrc || n.src || '', w: r.width, y: r.y};"
                        " }).filter(m => m.src && m.w >= 300 && m.y <= 700)"
                    )
                    new_found = False
                    for m in media:
                        if m["src"] in all_urls:
                            continue
                        all_urls.add(m["src"])
                        new_found = True
                        ext = "mp4" if m["tag"] == "VIDEO" else "jpg"
                        f_path = os.path.join(temp_dir, f"file_{len(all_urls)}.{ext}")
                        if await loop.run_in_executor(None, _download_sync, m["src"], f_path):
                            saved_files.append(f_path)

                    next_btn = await page.query_selector("button[aria-label*='Next'], button[aria-label*='Далее']")
                    if next_btn and await next_btn.is_visible():
                        await next_btn.click()
                        await asyncio.sleep(1.5)
                    elif not new_found:
                        break
            finally:
                try:
                    await asyncio.wait_for(browser.close(), timeout=15)
                except Exception:
                    pass
            return saved_files, caption