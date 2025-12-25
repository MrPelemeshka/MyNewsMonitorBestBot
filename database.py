import sqlite3
import hashlib
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Tuple
import logging

logger = logging.getLogger(__name__)

class Database:
    def __init__(self, db_path: str = 'news_bot.db'):
        self.db_path = db_path
        self.init_database()
    
    def get_connection(self) -> sqlite3.Connection:
        """Получение соединения с базой"""
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn
    
    def init_database(self):
        """Создание таблиц с индексами для ускорения запросов"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            # Пользователи
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # Каналы
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS user_channels (
                    user_id INTEGER,
                    channel_username TEXT,
                    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (user_id, channel_username)
                )
            ''')
            
            # Ключевые слова
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS user_keywords (
                    user_id INTEGER,
                    keyword TEXT,
                    is_negative INTEGER DEFAULT 0,
                    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (user_id, keyword, is_negative)
                )
            ''')
            
            # Отправленные новости
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS sent_news (
                    news_hash TEXT,
                    user_id INTEGER,
                    channel_username TEXT,
                    message_id INTEGER,
                    sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (news_hash, user_id)
                )
            ''')
            
            # Создаем индексы для ускорения поиска
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_sent_news_user ON sent_news(user_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_sent_news_hash ON sent_news(news_hash)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_channels_user ON user_channels(user_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_keywords_user ON user_keywords(user_id)")
            
            conn.commit()
    
    # === Методы для пользователей ===
    def add_user(self, user_id: int, username: str = None, first_name: str = None):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                '''INSERT OR IGNORE INTO users (user_id, username, first_name) 
                   VALUES (?, ?, ?)''',
                (user_id, username, first_name)
            )
            conn.commit()
    
    def get_user_stats(self, user_id: int) -> Dict[str, any]:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            cursor.execute(
                "SELECT COUNT(*) FROM user_channels WHERE user_id = ?",
                (user_id,)
            )
            channels_count = cursor.fetchone()[0]
            
            cursor.execute(
                "SELECT COUNT(*) FROM user_keywords WHERE user_id = ? AND is_negative = 0",
                (user_id,)
            )
            keywords_count = cursor.fetchone()[0]
            
            cursor.execute(
                "SELECT COUNT(*) FROM user_keywords WHERE user_id = ? AND is_negative = 1",
                (user_id,)
            )
            negative_count = cursor.fetchone()[0]
            
            cursor.execute(
                "SELECT COUNT(*) FROM sent_news WHERE user_id = ?",
                (user_id,)
            )
            sent_count = cursor.fetchone()[0]
            
            return {
                'channels': channels_count,
                'keywords': keywords_count,
                'negative_keywords': negative_count,
                'sent_news': sent_count
            }
    
    # === Методы для каналов ===
    def add_channel(self, user_id: int, channel: str) -> bool:
        """Добавляет канал для пользователя"""
        channel = channel.lstrip('@').lower()
        if not channel:
            return False
            
        with self.get_connection() as conn:
            cursor = conn.cursor()
            try:
                cursor.execute(
                    '''INSERT OR IGNORE INTO user_channels 
                       (user_id, channel_username) VALUES (?, ?)''',
                    (user_id, channel)
                )
                conn.commit()
                return cursor.rowcount > 0
            except Exception as e:
                logger.error(f"Ошибка добавления канала: {e}")
                return False
    
    def get_channels(self, user_id: int, page: int = 1, page_size: int = 10) -> Tuple[list, int, int]:
        """Получает список каналов пользователя с пагинацией"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            # Получаем общее количество каналов
            cursor.execute(
                "SELECT COUNT(*) as total FROM user_channels WHERE user_id = ?",
                (user_id,)
            )
            total_channels = cursor.fetchone()[0]
            
            # Рассчитываем offset
            offset = (page - 1) * page_size
            
            # Получаем каналы для текущей страницы
            cursor.execute(
                """SELECT channel_username 
                   FROM user_channels 
                   WHERE user_id = ? 
                   ORDER BY added_at DESC
                   LIMIT ? OFFSET ?""",
                (user_id, page_size, offset)
            )
            
            channels = [row['channel_username'] for row in cursor.fetchall()]
            
            # Рассчитываем общее количество страниц
            total_pages = (total_channels + page_size - 1) // page_size
            
            return channels, total_channels, total_pages
    
    def get_all_channels(self, user_id: int) -> list:
        """Получает все каналы пользователя (без пагинации)"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """SELECT channel_username 
                   FROM user_channels 
                   WHERE user_id = ? 
                   ORDER BY added_at""",
                (user_id,)
            )
            return [row['channel_username'] for row in cursor.fetchall()]
    
    def remove_channel(self, user_id: int, channel: str) -> bool:
        """Удаляет канал"""
        channel = channel.lstrip('@').lower()
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM user_channels WHERE user_id = ? AND channel_username = ?",
                (user_id, channel)
            )
            conn.commit()
            return cursor.rowcount > 0
    
    # === Методы для ключевых слов ===
    def set_keywords(self, user_id: int, keywords: list, is_negative: bool = False):
        """Устанавливает ключевые слова для пользователя"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            # Удаляем старые ключевые слова
            cursor.execute(
                "DELETE FROM user_keywords WHERE user_id = ? AND is_negative = ?",
                (user_id, 1 if is_negative else 0)
            )
            
            # Добавляем новые
            unique_keywords = set()
            for keyword in keywords:
                keyword = keyword.strip().lower()
                if keyword and keyword not in unique_keywords:
                    unique_keywords.add(keyword)
                    cursor.execute(
                        '''INSERT INTO user_keywords (user_id, keyword, is_negative) 
                           VALUES (?, ?, ?)''',
                        (user_id, keyword, 1 if is_negative else 0)
                    )
            
            conn.commit()
    
    def get_keywords(self, user_id: int) -> Tuple[list, list]:
        """Получает ключевые слова пользователя"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            cursor.execute(
                "SELECT keyword FROM user_keywords WHERE user_id = ? AND is_negative = 0",
                (user_id,)
            )
            keywords = [row['keyword'] for row in cursor.fetchall()]
            
            cursor.execute(
                "SELECT keyword FROM user_keywords WHERE user_id = ? AND is_negative = 1",
                (user_id,)
            )
            negative_keywords = [row['keyword'] for row in cursor.fetchall()]
            
            return keywords, negative_keywords
    
    # === Методы для новостей ===
    def generate_news_hash(self, text: str, channel: str, message_id: int = None) -> str:
        """Генерирует уникальный хэш для новости"""
        if message_id:
            content = f"{channel}:{message_id}".encode('utf-8')
        else:
            content = f"{channel}:{text[:500]}".encode('utf-8')
        return hashlib.md5(content).hexdigest()
    
    def is_news_sent(self, user_id: int, news_hash: str) -> bool:
        """Проверяет, была ли уже отправлена эта новость"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT 1 FROM sent_news WHERE user_id = ? AND news_hash = ? LIMIT 1",
                (user_id, news_hash)
            )
            return cursor.fetchone() is not None
    
    def mark_news_sent(self, user_id: int, news_hash: str, channel: str, message_id: int = None):
        """Отмечает новость как отправленную"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                '''INSERT OR IGNORE INTO sent_news 
                   (news_hash, user_id, channel_username, message_id) 
                   VALUES (?, ?, ?, ?)''',
                (news_hash, user_id, channel, message_id)
            )
            
            conn.commit()
    
    def cleanup_old_news(self, days: int = 30):
        """Очищает старые записи о новостях"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cutoff_date = datetime.now() - timedelta(days=days)
            cursor.execute(
                "DELETE FROM sent_news WHERE sent_at < ?",
                (cutoff_date,)
            )
            deleted_count = cursor.rowcount
            conn.commit()
            logger.info(f"Очищено {deleted_count} старых записей новостей")
            return deleted_count

# Глобальный объект БД
db = Database()