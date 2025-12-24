import sqlite3
import json
import hashlib
from datetime import datetime
from typing import List, Dict, Optional, Tuple

class Database:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.init_database()
    
    def init_database(self):
        """Инициализация всех таблиц"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            
            # Пользователи
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    is_active INTEGER DEFAULT 1
                )
            ''')
            
            # Ключевые слова пользователей
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS user_keywords (
                    user_id INTEGER,
                    keyword TEXT,
                    is_negative INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (user_id, keyword, is_negative),
                    FOREIGN KEY (user_id) REFERENCES users(user_id)
                )
            ''')
            
            # Каналы пользователей
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS user_channels (
                    user_id INTEGER,
                    channel_username TEXT,
                    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (user_id, channel_username),
                    FOREIGN KEY (user_id) REFERENCES users(user_id)
                )
            ''')
            
            # Отправленные новости (для дубликатов)
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS sent_news (
                    news_hash TEXT PRIMARY KEY,
                    user_id INTEGER,
                    channel_username TEXT,
                    message_id INTEGER,
                    sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users(user_id)
                )
            ''')
            
            # Статистика
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS stats (
                    user_id INTEGER,
                    date DATE,
                    news_sent INTEGER DEFAULT 0,
                    PRIMARY KEY (user_id, date),
                    FOREIGN KEY (user_id) REFERENCES users(user_id)
                )
            ''')
            
            conn.commit()
    
    # === Методы для пользователей ===
    
    def add_user(self, user_id: int):
        """Добавление нового пользователя"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            try:
                cursor.execute(
                    "INSERT OR IGNORE INTO users (user_id) VALUES (?)",
                    (user_id,)
                )
                
                # Добавляем ключевые слова по умолчанию
                from config import Config
                for keyword in Config.DEFAULT_KEYWORDS:
                    cursor.execute(
                        "INSERT OR IGNORE INTO user_keywords (user_id, keyword) VALUES (?, ?)",
                        (user_id, keyword)
                    )
                
                for keyword in Config.DEFAULT_NEGATIVE:
                    cursor.execute(
                        "INSERT OR IGNORE INTO user_keywords (user_id, keyword, is_negative) VALUES (?, ?, 1)",
                        (user_id, keyword)
                    )
                
                conn.commit()
            except Exception as e:
                print(f"Ошибка добавления пользователя: {e}")
    
    def get_user_channels(self, user_id: int) -> List[str]:
        """Получение каналов пользователя"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT channel_username FROM user_channels WHERE user_id = ? ORDER BY added_at",
                (user_id,)
            )
            return [row[0] for row in cursor.fetchall()]
    
    def add_user_channel(self, user_id: int, channel_username: str) -> bool:
        """Добавление канала пользователю"""
        channel_username = channel_username.lstrip('@')
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            try:
                cursor.execute(
                    "INSERT OR IGNORE INTO user_channels (user_id, channel_username) VALUES (?, ?)",
                    (user_id, channel_username)
                )
                conn.commit()
                return cursor.rowcount > 0
            except:
                return False
    
    def remove_user_channel(self, user_id: int, channel_username: str) -> bool:
        """Удаление канала у пользователя"""
        channel_username = channel_username.lstrip('@')
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM user_channels WHERE user_id = ? AND channel_username = ?",
                (user_id, channel_username)
            )
            conn.commit()
            return cursor.rowcount > 0
    
    # === Методы для ключевых слов ===
    
    def update_user_keywords(self, user_id: int, keywords: List[str], is_negative: bool = False):
        """Обновление ключевых слов пользователя"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            
            # Удаляем старые ключевые слова этого типа
            cursor.execute(
                "DELETE FROM user_keywords WHERE user_id = ? AND is_negative = ?",
                (user_id, 1 if is_negative else 0)
            )
            
            # Добавляем новые
            for keyword in keywords:
                cursor.execute(
                    "INSERT INTO user_keywords (user_id, keyword, is_negative) VALUES (?, ?, ?)",
                    (user_id, keyword.strip(), 1 if is_negative else 0)
                )
            
            conn.commit()
    
    def get_user_keywords(self, user_id: int) -> Tuple[List[str], List[str]]:
        """Получение ключевых слов пользователя"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            
            # Положительные ключевые слова
            cursor.execute(
                "SELECT keyword FROM user_keywords WHERE user_id = ? AND is_negative = 0",
                (user_id,)
            )
            keywords = [row[0] for row in cursor.fetchall()]
            
            # Отрицательные ключевые слова
            cursor.execute(
                "SELECT keyword FROM user_keywords WHERE user_id = ? AND is_negative = 1",
                (user_id,)
            )
            negative_keywords = [row[0] for row in cursor.fetchall()]
            
            return keywords, negative_keywords
    
    # === Методы для новостей и дубликатов ===
    
    def generate_news_hash(self, text: str, channel: str) -> str:
        """Генерация уникального хеша новости"""
        content = f"{channel}:{text[:200]}"
        return hashlib.md5(content.encode()).hexdigest()
    
    def is_news_sent(self, user_id: int, news_hash: str) -> bool:
        """Проверка, отправлялась ли новость пользователю"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT 1 FROM sent_news WHERE user_id = ? AND news_hash = ?",
                (user_id, news_hash)
            )
            return cursor.fetchone() is not None
    
    def mark_news_sent(self, user_id: int, news_hash: str, channel_username: str, message_id: int):
        """Отметка новости как отправленной"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                '''INSERT OR REPLACE INTO sent_news 
                   (news_hash, user_id, channel_username, message_id) 
                   VALUES (?, ?, ?, ?)''',
                (news_hash, user_id, channel_username, message_id)
            )
            
            # Обновляем статистику
            today = datetime.now().date()
            cursor.execute('''
                INSERT INTO stats (user_id, date, news_sent)
                VALUES (?, ?, 1)
                ON CONFLICT(user_id, date) DO UPDATE SET
                news_sent = news_sent + 1
            ''', (user_id, today))
            
            conn.commit()
    
    # === Методы для статистики ===
    
    def get_user_stats(self, user_id: int) -> Dict:
        """Получение статистики пользователя"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            
            # Общая статистика
            cursor.execute('''
                SELECT SUM(news_sent) FROM stats WHERE user_id = ?
            ''', (user_id,))
            total_news = cursor.fetchone()[0] or 0
            
            # Статистика за последние 7 дней
            cursor.execute('''
                SELECT date, news_sent FROM stats 
                WHERE user_id = ? AND date >= date('now', '-7 days')
                ORDER BY date DESC
            ''', (user_id,))
            last_week = cursor.fetchall()
            
            return {
                'total_news': total_news,
                'last_week': dict(last_week) if last_week else {},
                'channels_count': len(self.get_user_channels(user_id))
            }
    
    # === Административные методы ===
    
    def get_all_users(self) -> List[int]:
        """Получение всех пользователей"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT user_id FROM users WHERE is_active = 1")
            return [row[0] for row in cursor.fetchall()]
    
    def get_bot_stats(self) -> Dict:
        """Получение общей статистики бота"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            
            cursor.execute("SELECT COUNT(*) FROM users WHERE is_active = 1")
            total_users = cursor.fetchone()[0]
            
            cursor.execute("SELECT SUM(news_sent) FROM stats")
            total_news = cursor.fetchone()[0] or 0
            
            cursor.execute("SELECT COUNT(DISTINCT channel_username) FROM user_channels")
            total_channels = cursor.fetchone()[0] or 0
            
            return {
                'total_users': total_users,
                'total_news': total_news,
                'total_channels': total_channels
            }