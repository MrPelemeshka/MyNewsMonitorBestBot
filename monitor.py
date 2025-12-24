import asyncio
import logging
import re
from typing import List, Dict, Optional, Tuple
from telethon import TelegramClient, events
from telethon.tl.types import Message, Channel
from datetime import datetime, timedelta

from database import Database
from config import Config

logger = logging.getLogger(__name__)

class NewsMonitor:
    def __init__(self, api_id: int, api_hash: str, db: Database):
        self.api_id = api_id
        self.api_hash = api_hash
        self.db = db
        self.client = None
        self.connected = False
        
        # Кэш для быстрого доступа
        self.user_cache = {}
    
    async def connect(self):
        """Подключение к Telegram"""
        if not self.connected:
            self.client = TelegramClient('news_monitor_session', self.api_id, self.api_hash)
            await self.client.start()
            self.connected = True
            logger.info("✅ Подключились к Telegram для мониторинга")
    
    async def disconnect(self):
        """Отключение от Telegram"""
        if self.connected and self.client:
            await self.client.disconnect()
            self.connected = False
            logger.info("📴 Отключились от Telegram")
    
    def clean_text(self, text: str) -> str:
        """Очистка текста от мусора"""
        if not text:
            return ""
        
        # Удаляем ссылки
        text = re.sub(r'http\S+|www\S+|https\S+', '', text)
        # Удаляем упоминания и хештеги
        text = re.sub(r'[@#]\w+', '', text)
        # Удаляем лишние пробелы
        text = re.sub(r'\s+', ' ', text)
        return text.strip()
    
    def analyze_news(self, text: str, keywords: List[str], negative_keywords: List[str]) -> Dict:
        """Анализ новости на соответствие ключевым словам"""
        text_lower = text.lower()
        clean_text = self.clean_text(text).lower()
        
        # Проверяем длину
        if len(clean_text) < Config.MIN_NEWS_LENGTH:
            return {'relevant': False, 'reason': 'Текст слишком короткий'}
        
        if len(clean_text) > Config.MAX_NEWS_LENGTH:
            text_lower = text_lower[:Config.MAX_NEWS_LENGTH]
        
        # Ищем ключевые слова
        found_keywords = []
        for keyword in keywords:
            if keyword.lower() in text_lower:
                found_keywords.append(keyword)
        
        # Ищем отрицательные слова
        found_negative = []
        for neg_word in negative_keywords:
            if neg_word.lower() in text_lower:
                found_negative.append(neg_word)
        
        # Проверяем релевантность
        is_relevant = len(found_keywords) > 0 and len(found_negative) == 0
        
        return {
            'relevant': is_relevant,
            'keywords': found_keywords,
            'negative_keywords': found_negative,
            'reason': f"Найдено ключевых слов: {len(found_keywords)}" if is_relevant else "Не найдено ключевых слов"
        }
    
    async def check_channel_for_user(self, user_id: int, channel_username: str) -> List[Dict]:
        """Проверка канала для конкретного пользователя"""
        if not self.connected:
            await self.connect()
        
        try:
            # Получаем ключевые слова пользователя
            keywords, negative_keywords = self.db.get_user_keywords(user_id)
            
            if not keywords:  # Если нет ключевых слов, используем по умолчанию
                keywords = Config.DEFAULT_KEYWORDS
            
            # Получаем сущность канала
            channel_username = channel_username.lstrip('@')
            entity = await self.client.get_entity(channel_username)
            
            found_news = []
            
            # Получаем последние сообщения (последние 50)
            async for message in self.client.iter_messages(entity, limit=50):
                if message.text and len(message.text) > Config.MIN_NEWS_LENGTH:
                    # Анализируем сообщение
                    analysis = self.analyze_news(message.text, keywords, negative_keywords)
                    
                    if analysis['relevant']:
                        # Генерируем хеш для проверки дубликатов
                        news_hash = self.db.generate_news_hash(message.text, channel_username)
                        
                        # Проверяем, не отправляли ли уже
                        if not self.db.is_news_sent(user_id, news_hash):
                            found_news.append({
                                'text': message.text,
                                'channel': channel_username,
                                'keywords': analysis['keywords'],
                                'hash': news_hash,
                                'message_id': message.id,
                                'url': f"https://t.me/{channel_username}/{message.id}",
                                'date': message.date
                            })
            
            return found_news
            
        except Exception as e:
            logger.error(f"Ошибка проверки канала {channel_username} для пользователя {user_id}: {e}")
            return []
    
    async def check_all_users_channels(self, bot):
        """Проверка каналов для всех пользователей"""
        all_users = self.db.get_all_users()
        total_found = 0
        
        for user_id in all_users:
            try:
                user_channels = self.db.get_user_channels(user_id)
                
                if not user_channels:
                    continue
                
                for channel in user_channels:
                    # Проверяем канал
                    news_items = await self.check_channel_for_user(user_id, channel)
                    
                    # Отправляем найденные новости
                    for news in news_items:
                        sent = await self.send_news_to_user(bot, user_id, news)
                        if sent:
                            total_found += 1
                            
                            # Небольшая пауза между отправками
                            await asyncio.sleep(0.1)
                
            except Exception as e:
                logger.error(f"Ошибка мониторинга для пользователя {user_id}: {e}")
        
        return total_found
    
    async def send_news_to_user(self, bot, user_id: int, news: Dict) -> bool:
        """Отправка новости пользователю"""
        try:
            # Обрезаем текст если слишком длинный
            news_text = news['text']
            if len(news_text) > 3500:
                news_text = news_text[:3500] + "..."
            
            # Форматируем сообщение
            message_text = (
                f"📰 <b>{news['channel']}</b>\n\n"
                f"{news_text}\n\n"
            )
            
            if news.get('keywords'):
                message_text += f"🔍 <b>Ключевые слова:</b> {', '.join(news['keywords'][:3])}\n"
            
            if news.get('url'):
                message_text += f"\n🔗 <a href='{news['url']}'>Читать в канале</a>"
            
            # Отправляем сообщение
            sent_message = await bot.send_message(
                chat_id=user_id,
                text=message_text,
                parse_mode="HTML",
                disable_web_page_preview=False
            )
            
            # Отмечаем как отправленную
            self.db.mark_news_sent(
                user_id=user_id,
                news_hash=news['hash'],
                channel_username=news['channel'],
                message_id=sent_message.message_id
            )
            
            logger.info(f"📤 Отправлена новость пользователю {user_id} из {news['channel']}")
            return True
            
        except Exception as e:
            logger.error(f"Ошибка отправки пользователю {user_id}: {e}")
            return False
    
    async def get_channel_info(self, channel_username: str) -> Optional[Dict]:
        """Получение информации о канале"""
        if not self.connected:
            await self.connect()
        
        try:
            channel_username = channel_username.lstrip('@')
            entity = await self.client.get_entity(channel_username)
            
            # Получаем несколько последних сообщений для анализа
            last_messages = []
            async for message in self.client.iter_messages(entity, limit=5):
                if message.text:
                    last_messages.append(message.text[:100])
            
            return {
                'username': channel_username,
                'title': getattr(entity, 'title', 'Неизвестно'),
                'participants_count': getattr(entity, 'participants_count', 0),
                'last_messages': last_messages,
                'is_channel': isinstance(entity, Channel)
            }
            
        except Exception as e:
            logger.error(f"Ошибка получения информации о канале {channel_username}: {e}")
            return None