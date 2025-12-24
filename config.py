import os
from dotenv import load_dotenv

load_dotenv()

class Config:
    # Telegram API
    BOT_TOKEN = os.getenv("BOT_TOKEN")
    ADMIN_ID = int(os.getenv("ADMIN_ID", 0))
    
    # Настройки прокси для PythonAnywhere
    PROXY_URL = "http://proxy.server:3128"
    
    # Настройки базы данных
    DB_PATH = "news_bot.db"
    
    # Настройки мониторинга
    CHECK_INTERVAL = 300  # 5 минут между проверками
    MAX_NEWS_LENGTH = 4000  # Максимальная длина новости
    MIN_NEWS_LENGTH = 50    # Минимальная длина новости
    
    # Ключевые слова по умолчанию
    DEFAULT_KEYWORDS = ["технологии", "программирование", "стартап", "инвестиции", "ии", "ai"]
    DEFAULT_NEGATIVE = ["смерть", "авария", "преступление", "война", "скандал"]
    
    @classmethod
    def validate(cls):
        if not cls.BOT_TOKEN:
            raise ValueError("Не задан BOT_TOKEN в .env файле")