import os
import asyncio
import logging
from aiogram import Bot, Dispatcher
from config import API_KEY, TEMP_BASE_DIR
from handlers import router

logging.basicConfig(
    level=logging.INFO, 
    format="%(asctime)s - %(levelname)s - %(message)s"
)

async def main():
    os.makedirs(TEMP_BASE_DIR, exist_ok=True)
    
    bot = Bot(token=API_KEY)
    dp = Dispatcher()
    dp.include_router(router)
    
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())