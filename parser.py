import aiohttp
import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Optional, Tuple
import backoff
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

class SmartCache:
    """Умное кэширование с TTL и ограничением размера"""
    def __init__(self, ttl: int = 300, max_size: int = 1000):
        self.cache = {}
        self.ttl = ttl
        self.max_size = max_size
        self.hits = 0
        self.misses = 0
        self.stats = {'hits': 0, 'misses': 0, 'evictions': 0}
    
    def get(self, key: str):
        """Получение из кэша"""
        if key in self.cache:
            cached = self.cache[key]
            if datetime.now() - cached['timestamp'] < timedelta(seconds=self.ttl):
                self.stats['hits'] += 1
                return cached['data']
            else:
                # Удаляем просроченный элемент
                del self.cache[key]
        self.stats['misses'] += 1
        return None
    
    def set(self, key: str, data):
        """Сохранение в кэш"""
        if len(self.cache) >= self.max_size:
            # Удаляем самые старые записи
            oldest_key = min(self.cache.keys(), 
                           key=lambda k: self.cache[k]['timestamp'])
            del self.cache[oldest_key]
            self.stats['evictions'] += 1
        
        self.cache[key] = {
            'data': data,
            'timestamp': datetime.now()
        }
    
    def clear(self):
        """Очистка кэша"""
        self.cache.clear()
        self.stats = {'hits': 0, 'misses': 0, 'evictions': 0}
    
    def get_stats(self) -> Dict:
        """Статистика кэша"""
        return {
            **self.stats,
            'size': len(self.cache),
            'hit_rate': self.stats['hits'] / max((self.stats['hits'] + self.stats['misses']), 1)
        }


class EnhancedTelegramParser:
    """Улучшенный парсер Telegram с кэшированием и обработкой ошибок"""
    def __init__(self, max_retries: int = 3, cache_ttl: int = 300):
        self.base_url = "https://t.me/s/"
        self.session: Optional[aiohttp.ClientSession] = None
        self.max_retries = max_retries
        self.cache = SmartCache(ttl=cache_ttl, max_size=1000)
        self.request_stats = {'success': 0, 'failures': 0, 'timeouts': 0}
        self.start_time = datetime.now()
        
    async def init_session(self):
        """Инициализация сессии"""
        if not self.session or self.session.closed:
            timeout = aiohttp.ClientTimeout(total=30, connect=10, sock_read=15)
            connector = aiohttp.TCPConnector(limit=10, limit_per_host=2)
            self.session = aiohttp.ClientSession(
                timeout=timeout,
                connector=connector,
                headers={
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
                    'Accept-Language': 'ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7',
                    'Accept-Encoding': 'gzip, deflate, br',
                    'DNT': '1',
                    'Connection': 'keep-alive',
                    'Upgrade-Insecure-Requests': '1',
                    'Sec-Fetch-Dest': 'document',
                    'Sec-Fetch-Mode': 'navigate',
                    'Sec-Fetch-Site': 'none',
                    'Sec-Fetch-User': '?1',
                    'Cache-Control': 'max-age=0',
                }
            )
    
    async def close_session(self):
        """Закрытие сессии"""
        if self.session and not self.session.closed:
            await self.session.close()
            self.session = None
    
    @backoff.on_exception(
        backoff.expo,
        (aiohttp.ClientError, asyncio.TimeoutError),
        max_tries=3,
        max_time=30
    )
    async def fetch_with_retry(self, url: str) -> Optional[str]:
        """Получение HTML с повторными попытками"""
        await self.init_session()
        
        try:
            async with self.session.get(url, allow_redirects=True) as response:
                if response.status == 200:
                    html = await response.text()
                    self.request_stats['success'] += 1
                    return html
                elif response.status == 404:
                    logger.warning(f"Ресурс не найден: {url}")
                    return None
                else:
                    logger.error(f"Ошибка {response.status} при запросе {url}")
                    self.request_stats['failures'] += 1
                    return None
        except asyncio.TimeoutError:
            logger.error(f"Таймаут при запросе {url}")
            self.request_stats['timeouts'] += 1
            raise
        except Exception as e:
            logger.error(f"Ошибка при запросе {url}: {e}")
            self.request_stats['failures'] += 1
            raise
    
    async def fetch_channel(self, channel_username: str, use_cache: bool = True) -> Optional[str]:
        """Получение HTML страницы канала с кэшированием"""
        channel = channel_username.lstrip('@')
        cache_key = f"html:{channel}"
        
        # Проверяем кэш
        if use_cache:
            cached_html = self.cache.get(cache_key)
            if cached_html:
                return cached_html
        
        url = f"{self.base_url}{channel}"
        html = await self.fetch_with_retry(url)
        
        if html and use_cache:
            self.cache.set(cache_key, html)
        
        return html
    
    async def get_fresh_messages(self, channel_username: str, hours: int = 24, limit: int = 30) -> List[Dict]:
        """Получение только свежих сообщений"""
        all_messages = await self.get_channel_messages(channel_username, limit=limit)
        # Используем offset-naive datetime для сравнения
        cutoff = datetime.now() - timedelta(hours=hours)
        
        fresh_messages = []
        for msg in all_messages:
            if msg.get('timestamp'):
                # Приводим все даты к offset-naive формату для сравнения
                msg_time = msg['timestamp']
                if msg_time.tzinfo is not None:
                    # Если дата с часовым поясом, конвертируем в локальное время
                    msg_time = msg_time.astimezone(timezone.utc).replace(tzinfo=None)
                
                if msg_time > cutoff:
                    # Сохраняем оригинальную дату
                    msg['timestamp_naive'] = msg_time
                    fresh_messages.append(msg)
        
        logger.info(f"Найдено {len(fresh_messages)} свежих сообщений в @{channel_username} (за {hours}ч)")
        return fresh_messages
    
    async def get_channel_messages(self, channel_username: str, limit: int = 30) -> List[Dict]:
        """Получение сообщений из канала"""
        html = await self.fetch_channel(channel_username)
        if not html:
            return []
        
        try:
            soup = BeautifulSoup(html, 'html.parser')
            messages = []
            
            # Ищем все сообщения
            message_widgets = soup.find_all('div', class_='tgme_widget_message')
            
            for widget in message_widgets[:limit]:
                message_data = await self._parse_message_enhanced(widget, channel_username.lstrip('@'))
                if message_data:
                    messages.append(message_data)
            
            # Сортируем по времени (новые сверху)
            messages.sort(key=lambda x: x.get('timestamp') or datetime.min, reverse=True)
            
            logger.info(f"Найдено {len(messages)} сообщений в @{channel_username}")
            return messages
            
        except Exception as e:
            logger.error(f"Ошибка парсинга @{channel_username}: {e}")
            return []
    
    async def _parse_message_enhanced(self, widget, channel: str) -> Optional[Dict]:
        """Улучшенный парсинг одного сообщения с сохранением форматирования"""
        try:
            # Извлекаем текст с сохранением форматирования
            text_widget = widget.find('div', class_='tgme_widget_message_text')
            message_text = ""
            if text_widget:
                message_text = text_widget.get_text(separator='\n', strip=True)
            
            # Проверяем наличие файлов разного типа
            has_file = False
            file_types = []
            
            # Проверяем фото
            photo_widget = widget.find('a', class_='tgme_widget_message_photo')
            if photo_widget:
                has_file = True
                file_types.append('photo')
            
            # Проверяем видео
            video_widget = widget.find('div', class_='tgme_widget_message_video')
            if video_widget:
                has_file = True
                file_types.append('video')
            
            # Проверяем документы
            document_widget = widget.find('a', class_='tgme_widget_message_document')
            if document_widget:
                has_file = True
                file_types.append('document')
            
            # Проверяем аудио (если есть такой класс)
            audio_widget = widget.find('div', class_='tgme_widget_message_audio')
            if audio_widget:
                has_file = True
                file_types.append('audio')
            
            # Проверяем голосовые сообщения
            voice_widget = widget.find('div', class_='tgme_widget_message_voice')
            if voice_widget:
                has_file = True
                file_types.append('voice')
            
            # Проверяем стикеры
            sticker_widget = widget.find('div', class_='tgme_widget_message_sticker')
            if sticker_widget:
                has_file = True
                file_types.append('sticker')
            
            # Если нет текста и нет файлов, пропускаем
            if not message_text and not has_file:
                return None
            
            # Извлекаем время
            time_widget = widget.find('time', class_='time')
            message_time = None
            if time_widget and 'datetime' in time_widget.attrs:
                try:
                    time_str = time_widget['datetime']
                    
                    if time_str.endswith('Z'):
                        time_str = time_str[:-1] + '+00:00'
                        message_time = datetime.fromisoformat(time_str)
                    else:
                        if '+' in time_str or '-' in time_str:
                            message_time = datetime.fromisoformat(time_str)
                        else:
                            time_str = time_str + '+00:00'
                            message_time = datetime.fromisoformat(time_str)
                    
                    if message_time.tzinfo is not None:
                        message_time = message_time.astimezone(timezone.utc).replace(tzinfo=None)
                        
                except Exception as e:
                    logger.debug(f"Ошибка парсинга времени: {e}")
                    message_time = datetime.now()
            
            # Извлекаем ID сообщения
            message_id = None
            message_url = None
            
            link_widget = widget.find('a', class_='tgme_widget_message_date')
            if link_widget and 'href' in link_widget.attrs:
                href = link_widget['href']
                match = re.search(r'/(\d+)(?:\?|$)', href)
                if match:
                    message_id = int(match.group(1))
                    message_url = href
            
            # Извлекаем количество просмотров (если есть)
            views_widget = widget.find('span', class_='tgme_widget_message_views')
            views = None
            if views_widget:
                views_text = views_widget.get_text(strip=True)
                views_text = re.sub(r'\D', '', views_text)
                if views_text.isdigit():
                    views = int(views_text)
            
            return {
                'text': message_text,
                'timestamp': message_time,
                'id': message_id,
                'url': message_url,
                'channel': channel,
                'parsed_at': datetime.now(),
                'has_file': has_file,
                'file_types': file_types,
                'has_media': has_file,  # Для обратной совместимости
                'views': views,
                'length': len(message_text)
            }
            
        except Exception as e:
            logger.debug(f"Ошибка парсинга сообщения: {e}")
            return None
    
    async def check_channel_exists(self, channel_username: str) -> Tuple[bool, str]:
        """Проверка существования канала с дополнительной информацией"""
        channel = channel_username.lstrip('@')
        
        try:
            html = await self.fetch_channel(channel, use_cache=False)
            
            if not html:
                return False, "Не удалось загрузить страницу"
            
            soup = BeautifulSoup(html, 'html.parser')
            
            # Проверяем на ошибки
            page_text = soup.get_text().lower()
            
            if 'доступ запрещен' in page_text or 'закрытый канал' in page_text:
                return False, "Канал приватный или доступ запрещен"
            
            if 'не существует' in page_text or 'отсутствует' in page_text:
                return False, "Канал не существует"
            
            if 'указан неверно' in page_text:
                return False, "Некорректный username канала"
            
            # Проверяем наличие сообщений
            messages = soup.find_all('div', class_='tgme_widget_message')
            
            if len(messages) > 0:
                # Получаем информацию о канале
                channel_title = soup.find('div', class_='tgme_channel_info_header_title')
                title = channel_title.get_text(strip=True) if channel_title else "Неизвестно"
                
                # Очищаем текст от лишних пробелов и переносов
                title = ' '.join(title.split())
                
                channel_description = soup.find('div', class_='tgme_channel_info_description')
                description = channel_description.get_text(strip=True, separator=' ') if channel_description else ""
                
                info = f"✅ Канал найден: {title}"
                if description:
                    info += f"\n\n{description[:150]}..."
                
                return True, info
            else:
                return False, "На канале нет сообщений или он пуст"
                
        except Exception as e:
            logger.error(f"Ошибка проверки канала @{channel}: {e}")
            return False, f"Ошибка проверки: {str(e)[:100]}"
    
    def get_stats(self) -> Dict:
        """Получение статистики парсера"""
        return {
            **self.request_stats,
            'cache_stats': self.cache.get_stats(),
            'uptime': str(datetime.now() - self.start_time)
        }
    
    async def __aenter__(self):
        self.start_time = datetime.now()
        await self.init_session()
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close_session()

# Синглтон парсера
parser = EnhancedTelegramParser()