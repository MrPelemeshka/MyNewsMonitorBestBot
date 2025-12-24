import asyncio
import logging
import aiohttp
import sqlite3
import hashlib
import re
from datetime import datetime, timedelta
from typing import List, Dict, Optional
from bs4 import BeautifulSoup

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, CommandObject
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

# === ВАШИ ДАННЫЕ ===
BOT_TOKEN = "8377696397:AAFi8gsJlXIZsjgxzC4SoCnwqqtVzUk3oms"
ADMIN_ID = 7261954639
# ===================

# Настройки прокси для PythonAnywhere
PROXY_AUTH = aiohttp.BasicAuth('proxyuser', 'proxyuser')
PROXY_URL = "http://proxy.server:3128"

# Состояния для FSM
class UserStates(StatesGroup):
    waiting_for_keywords = State()
    waiting_for_negative = State()
    waiting_for_custom_period = State()

# ==================== ТЕЛЕГРАМ WEB ПАРСЕР ====================

class TelegramWebParser:
    """Парсер публичных Telegram каналов через веб-интерфейс"""
    
    def __init__(self):
        self.base_url = "https://t.me/s/"
        self.session = None
        
    async def init_session(self):
        """Инициализация aiohttp сессии"""
        if not self.session:
            connector = aiohttp.TCPConnector(ssl=False)
            timeout = aiohttp.ClientTimeout(total=30)
            self.session = aiohttp.ClientSession(
                connector=connector,
                timeout=timeout,
                headers={
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
                }
            )
    
    async def close_session(self):
        """Закрытие сессии"""
        if self.session:
            await self.session.close()
            self.session = None
    
    async def get_channel_messages(self, channel_username: str, limit: int = 50) -> List[Dict]:
        """
        Получение сообщений из публичного Telegram канала
        
        Args:
            channel_username: username канала (без @)
            limit: максимальное количество сообщений для парсинга
        """
        await self.init_session()
        
        channel = channel_username.lstrip('@')
        url = f"{self.base_url}{channel}"
        
        try:
            # Используем прокси PythonAnywhere
            proxy_auth = aiohttp.BasicAuth('proxyuser', 'proxyuser')
            
            async with self.session.get(
                url, 
                proxy=PROXY_URL,
                proxy_auth=proxy_auth,
                timeout=30
            ) as response:
                if response.status != 200:
                    logging.error(f"Ошибка {response.status} для {url}")
                    return []
                
                html_content = await response.text()
                
                # Парсим HTML
                soup = BeautifulSoup(html_content, 'html.parser')
                
                # Находим все сообщения
                messages = []
                message_widgets = soup.find_all('div', class_='tgme_widget_message')
                
                for widget in message_widgets[:limit]:
                    message_data = self._parse_message_widget(widget, channel)
                    if message_data:
                        messages.append(message_data)
                
                # Сортируем по времени (новые сверху)
                messages.sort(key=lambda x: x['timestamp'] if x['timestamp'] else datetime.min, reverse=True)
                
                return messages
                
        except Exception as e:
            logging.error(f"Ошибка парсинга канала {channel}: {e}")
            return []
    
    def _parse_message_widget(self, widget, channel: str) -> Optional[Dict]:
        """Парсинг отдельного виджета сообщения"""
        try:
            # Извлекаем текст сообщения
            text_widget = widget.find('div', class_='tgme_widget_message_text')
            if not text_widget:
                return None
            
            # Получаем чистый текст
            message_text = text_widget.get_text(separator='\n', strip=True)
            if not message_text or len(message_text) < 30:  # Слишком короткие пропускаем
                return None
            
            # Извлекаем время сообщения
            time_widget = widget.find('time', class_='time')
            message_time = None
            if time_widget and 'datetime' in time_widget.attrs:
                try:
                    time_str = time_widget['datetime']
                    # Убираем 'Z' и добавляем временную зону UTC
                    time_str = time_str.replace('Z', '+00:00')
                    message_time = datetime.fromisoformat(time_str)
                    # Конвертируем в локальное время
                    message_time = message_time.astimezone()
                except Exception as e:
                    logging.debug(f"Ошибка парсинга времени: {e}")
                    message_time = None
            
            # Извлекаем ID сообщения
            message_id = None
            link_widget = widget.find('a', class_='tgme_widget_message_date')
            if link_widget and 'href' in link_widget.attrs:
                href = link_widget['href']
                match = re.search(r'/(\d+)$', href)
                if match:
                    message_id = int(match.group(1))
            
            # Формируем URL сообщения
            message_url = None
            if message_id:
                message_url = f"https://t.me/{channel}/{message_id}"
            
            return {
                'text': message_text,
                'timestamp': message_time,
                'id': message_id,
                'url': message_url,
                'channel': channel,
                'parsed_at': datetime.now()
            }
            
        except Exception as e:
            logging.error(f"Ошибка парсинга виджета: {e}")
            return None
    
    def filter_messages_by_time(self, messages: List[Dict], hours: int = 24) -> List[Dict]:
        """Фильтрация сообщений по времени"""
        if hours <= 0:  # 0 = все сообщения
            return messages
        
        cutoff_time = datetime.now() - timedelta(hours=hours)
        # Убедимся, что cutoff_time имеет тот же часовой пояс
        if cutoff_time.tzinfo is None:
            cutoff_time = cutoff_time.replace(tzinfo=datetime.now().astimezone().tzinfo)
        
        filtered = []
        
        for msg in messages:
            msg_time = msg.get('timestamp')
            
            # Если время не определено, включаем сообщение
            if not msg_time:
                filtered.append(msg)
                continue
            
            # Убедимся, что оба времени имеют часовой пояс
            if msg_time.tzinfo is None:
                # Если у сообщения нет часового пояса, считаем его локальным
                msg_time = msg_time.replace(tzinfo=datetime.now().astimezone().tzinfo)
            
            if msg_time >= cutoff_time:
                filtered.append(msg)
        
        return filtered

# ==================== БАЗА ДАННЫХ ====================

class NewsBotDB:
    """База данных для бота"""
    
    def __init__(self, db_path: str = 'news_bot_web.db'):
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.init_db()
    
    def init_db(self):
        cursor = self.conn.cursor()
        
        # Пользователи
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_check TIMESTAMP
            )
        ''')
        
        # Каналы пользователя
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS user_channels (
                user_id INTEGER,
                channel_username TEXT,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                is_active INTEGER DEFAULT 1,
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
        
        # История проверок
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS check_history (
                user_id INTEGER,
                check_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                period_hours INTEGER,
                channels_checked INTEGER,
                news_found INTEGER,
                success INTEGER DEFAULT 1
            )
        ''')
        
        self.conn.commit()
    
    # === Методы для пользователей ===
    def add_user(self, user_id: int, username: str = None, first_name: str = None):
        cursor = self.conn.cursor()
        cursor.execute(
            '''INSERT OR IGNORE INTO users (user_id, username, first_name) 
               VALUES (?, ?, ?)''',
            (user_id, username, first_name)
        )
        self.conn.commit()
    
    def update_last_check(self, user_id: int):
        cursor = self.conn.cursor()
        cursor.execute(
            "UPDATE users SET last_check = CURRENT_TIMESTAMP WHERE user_id = ?",
            (user_id,)
        )
        self.conn.commit()
    
    # === Методы для каналов ===
    def add_channel(self, user_id: int, channel: str) -> bool:
        cursor = self.conn.cursor()
        channel = channel.lstrip('@')
        try:
            cursor.execute(
                '''INSERT OR IGNORE INTO user_channels 
                   (user_id, channel_username, is_active) VALUES (?, ?, 1)''',
                (user_id, channel)
            )
            self.conn.commit()
            return cursor.rowcount > 0
        except:
            return False
    
    def get_channels(self, user_id: int, active_only: bool = True) -> list:
        cursor = self.conn.cursor()
        if active_only:
            cursor.execute(
                "SELECT channel_username FROM user_channels WHERE user_id = ? AND is_active = 1 ORDER BY added_at",
                (user_id,)
            )
        else:
            cursor.execute(
                "SELECT channel_username FROM user_channels WHERE user_id = ? ORDER BY added_at",
                (user_id,)
            )
        return [row[0] for row in cursor.fetchall()]
    
    def remove_channel(self, user_id: int, channel: str) -> bool:
        cursor = self.conn.cursor()
        channel = channel.lstrip('@')
        cursor.execute(
            "DELETE FROM user_channels WHERE user_id = ? AND channel_username = ?",
            (user_id, channel)
        )
        self.conn.commit()
        return cursor.rowcount > 0
    
    def deactivate_channel(self, user_id: int, channel: str):
        """Деактивация канала вместо удаления"""
        cursor = self.conn.cursor()
        channel = channel.lstrip('@')
        cursor.execute(
            "UPDATE user_channels SET is_active = 0 WHERE user_id = ? AND channel_username = ?",
            (user_id, channel)
        )
        self.conn.commit()
    
    # === Методы для ключевых слов ===
    def set_keywords(self, user_id: int, keywords: list, is_negative: bool = False):
        cursor = self.conn.cursor()
        
        # Удаляем старые ключевые слова этого типа
        cursor.execute(
            "DELETE FROM user_keywords WHERE user_id = ? AND is_negative = ?",
            (user_id, 1 if is_negative else 0)
        )
        
        # Добавляем новые
        for keyword in keywords:
            keyword = keyword.strip().lower()
            if keyword:
                cursor.execute(
                    '''INSERT INTO user_keywords (user_id, keyword, is_negative) 
                       VALUES (?, ?, ?)''',
                    (user_id, keyword, 1 if is_negative else 0)
                )
        
        self.conn.commit()
    
    def get_keywords(self, user_id: int) -> tuple:
        cursor = self.conn.cursor()
        
        cursor.execute(
            "SELECT keyword FROM user_keywords WHERE user_id = ? AND is_negative = 0",
            (user_id,)
        )
        keywords = [row[0] for row in cursor.fetchall()]
        
        cursor.execute(
            "SELECT keyword FROM user_keywords WHERE user_id = ? AND is_negative = 1",
            (user_id,)
        )
        negative_keywords = [row[0] for row in cursor.fetchall()]
        
        return keywords, negative_keywords
    
    # === Методы для новостей ===
    def generate_news_hash(self, text: str, channel: str, message_id: int = None) -> str:
        """Создает уникальный хеш для новости"""
        if message_id:
            content = f"{channel}:{message_id}".encode('utf-8')
        else:
            content = f"{channel}:{text[:200]}".encode('utf-8')
        return hashlib.md5(content).hexdigest()
    
    def is_news_sent(self, user_id: int, news_hash: str) -> bool:
        cursor = self.conn.cursor()
        cursor.execute(
            "SELECT 1 FROM sent_news WHERE user_id = ? AND news_hash = ?",
            (user_id, news_hash)
        )
        return cursor.fetchone() is not None
    
    def mark_news_sent(self, user_id: int, news_hash: str, channel: str, message_id: int = None):
        cursor = self.conn.cursor()
        cursor.execute(
            '''INSERT OR IGNORE INTO sent_news 
               (news_hash, user_id, channel_username, message_id) 
               VALUES (?, ?, ?, ?)''',
            (news_hash, user_id, channel, message_id)
        )
        self.conn.commit()
    
    # === Методы для статистики ===
    def add_check_history(self, user_id: int, period_hours: int, 
                         channels_checked: int, news_found: int, success: bool = True):
        cursor = self.conn.cursor()
        cursor.execute(
            '''INSERT INTO check_history 
               (user_id, period_hours, channels_checked, news_found, success)
               VALUES (?, ?, ?, ?, ?)''',
            (user_id, period_hours, channels_checked, news_found, 1 if success else 0)
        )
        self.conn.commit()
    
    def get_user_stats(self, user_id: int) -> dict:
        cursor = self.conn.cursor()
        
        cursor.execute(
            "SELECT COUNT(*) FROM user_channels WHERE user_id = ? AND is_active = 1",
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
        news_received = cursor.fetchone()[0]
        
        # Последняя проверка
        cursor.execute(
            "SELECT check_time, period_hours, news_found FROM check_history WHERE user_id = ? ORDER BY check_time DESC LIMIT 1",
            (user_id,)
        )
        last_check = cursor.fetchone()
        
        return {
            'channels': channels_count,
            'keywords': keywords_count,
            'negative': negative_count,
            'news_received': news_received,
            'last_check': last_check
        }

# Инициализация
db = NewsBotDB()
parser = TelegramWebParser()

# ==================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ====================

def analyze_message(text: str, keywords: List[str], negative_keywords: List[str]) -> Dict:
    """Анализ сообщения на соответствие ключевым словам"""
    text_lower = text.lower()
    
    # Проверяем ключевые слова
    found_keywords = []
    for keyword in keywords:
        if keyword.lower() in text_lower:
            found_keywords.append(keyword)
    
    # Проверяем слова-исключения
    found_negative = []
    for neg_keyword in negative_keywords:
        if neg_keyword.lower() in text_lower:
            found_negative.append(neg_keyword)
    
    return {
        'has_keywords': len(found_keywords) > 0,
        'has_negative': len(found_negative) > 0,
        'keywords': found_keywords,
        'negative': found_negative,
        'relevant': len(found_keywords) > 0 and len(found_negative) == 0
    }

def format_period_text(hours: int) -> str:
    """Форматирование текста периода"""
    if hours == 0:
        return "всю историю"
    elif hours == 1:
        return "последний час"
    elif hours < 24:
        return f"последние {hours} часов"
    elif hours == 24:
        return "последние 24 часа"
    elif hours < 168:  # 7 дней
        days = hours // 24
        return f"последние {days} дней"
    else:
        weeks = hours // 168
        return f"последние {weeks} недель"

def get_period_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура для выбора периода"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🕐 1 час", callback_data="period:1"),
            InlineKeyboardButton(text="🕑 3 часа", callback_data="period:3"),
            InlineKeyboardButton(text="🕒 6 часов", callback_data="period:6"),
        ],
        [
            InlineKeyboardButton(text="🕓 12 часов", callback_data="period:12"),
            InlineKeyboardButton(text="🕔 24 часа", callback_data="period:24"),
            InlineKeyboardButton(text="🕕 3 дня", callback_data="period:72"),
        ],
        [
            InlineKeyboardButton(text="🕖 Неделя", callback_data="period:168"),
            InlineKeyboardButton(text="🕗 Всегда", callback_data="period:0"),
            InlineKeyboardButton(text="✏️ Свое", callback_data="period:custom"),
        ]
    ])

# ==================== ОСНОВНОЙ БОТ ====================

async def main():
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    logger = logging.getLogger(__name__)
    
    logger.info("🚀 Запуск бота с Telegram Web парсингом...")
    
    # Создаем сессию с прокси
    session = AiohttpSession(proxy=(PROXY_URL, PROXY_AUTH))
    bot = Bot(token=BOT_TOKEN, session=session)
    dp = Dispatcher(storage=MemoryStorage())
    
    # ==================== КЛАВИАТУРЫ ====================
    
    def get_main_keyboard() -> ReplyKeyboardMarkup:
        return ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="🔍 Проверить новости"), KeyboardButton(text="📊 Статистика")],
                [KeyboardButton(text="📢 Мои каналы"), KeyboardButton(text="🏷️ Мои теги")],
                [KeyboardButton(text="➕ Добавить канал"), KeyboardButton(text="⚙️ Настройки")]
            ],
            resize_keyboard=True,
            input_field_placeholder="Выберите действие..."
        )
    
    # ==================== КОМАНДЫ БОТА ====================
    
    @dp.message(Command("start"))
    async def cmd_start(message: Message):
        """Команда /start"""
        user_id = message.from_user.id
        db.add_user(user_id, message.from_user.username, message.from_user.first_name)
        
        welcome_text = (
            f"👋 <b>Привет, {message.from_user.first_name}!</b>\n\n"
            f"🤖 Я бот для мониторинга Telegram-каналов.\n\n"
            f"<b>📡 Использую:</b> Telegram Web парсинг\n"
            f"<b>✅ Работает на:</b> PythonAnywhere\n"
            f"<b>🎯 Проверяю:</b> публичные каналы\n\n"
            f"<b>Начните с кнопки '➕ Добавить канал'</b>"
        )
        
        await message.answer(welcome_text, parse_mode="HTML", reply_markup=get_main_keyboard())
    
    @dp.message(F.text == "🔍 Проверить новости")
    async def cmd_check_news(message: Message):
        """Проверка новостей - выбор периода"""
        user_id = message.from_user.id
        
        # Проверяем, есть ли каналы
        channels = db.get_channels(user_id)
        if not channels:
            await message.answer(
                "❌ <b>У вас нет каналов для проверки</b>\n\n"
                "Добавьте каналы через кнопку '➕ Добавить канал'",
                parse_mode="HTML"
            )
            return
        
        # Проверяем, есть ли ключевые слова
        keywords, _ = db.get_keywords(user_id)
        if not keywords:
            await message.answer(
                "⚠️ <b>У вас не заданы ключевые слова</b>\n\n"
                "Используются значения по умолчанию:\n"
                "<code>технологии, программирование, стартап</code>\n\n"
                "Настройте теги через кнопку '🏷️ Мои теги'",
                parse_mode="HTML"
            )
        
        await message.answer(
            "🔍 <b>За какой период проверять новости?</b>\n\n"
            "Выберите период времени для поиска:",
            parse_mode="HTML",
            reply_markup=get_period_keyboard()
        )
    
    @dp.callback_query(F.data.startswith("period:"))
    async def callback_period_selected(callback: types.CallbackQuery, state: FSMContext):
        """Обработка выбора периода"""
        period_data = callback.data.split(":")[1]
        
        if period_data == "custom":
            await callback.message.edit_text(
                "✏️ <b>Введите количество часов:</b>\n\n"
                "Примеры:\n"
                "• <code>2</code> - за последние 2 часа\n"
                "• <code>48</code> - за последние 2 дня\n"
                "• <code>0</code> - всю историю (все доступные сообщения)\n\n"
                "Или отправьте 'отмена' для отмены",
                parse_mode="HTML"
            )
            await state.set_state(UserStates.waiting_for_custom_period)
        else:
            try:
                hours = int(period_data)
                await start_news_check(callback.message, hours, callback.from_user.id)
                await callback.message.delete()
            except ValueError:
                await callback.answer("❌ Ошибка выбора периода")
        
        await callback.answer()
    
    @dp.message(UserStates.waiting_for_custom_period)
    async def process_custom_period(message: Message, state: FSMContext):
        """Обработка введенного периода"""
        if message.text.lower() in ['отмена', 'cancel', 'назад']:
            await message.answer("❌ Ввод периода отменен", reply_markup=get_main_keyboard())
            await state.clear()
            return
        
        try:
            hours = int(message.text.strip())
            if hours < 0:
                raise ValueError
            
            await start_news_check(message, hours, message.from_user.id)
            await state.clear()
            
        except ValueError:
            await message.answer(
                "❌ <b>Некорректное значение</b>\n\n"
                "Введите целое число (часы):\n"
                "<code>12</code> - за 12 часов\n"
                "<code>0</code> - всю историю",
                parse_mode="HTML"
            )
    
    @dp.message(UserStates.waiting_for_keywords)
    async def process_keywords_input(message: Message, state: FSMContext):
        """Обработка ввода ключевых слов"""
        if not message.text.strip():
            await message.answer("❌ Вы отправили пустое сообщение. Пожалуйста, введите слова через запятую.")
            return
    
        # Очищаем и разбиваем ввод пользователя
        raw_keywords = [word.strip().lower() for word in message.text.split(',')]
        keywords = [word for word in raw_keywords if word]  # Убираем пустые строки
    
        if not keywords:
            await message.answer("❌ Не найдено ключевых слов. Попробуйте снова. Пример: <code>технологии, программирование</code>", parse_mode="HTML")
            return
    
        # Сохраняем в базу данных
        user_id = message.from_user.id
        db.set_keywords(user_id, keywords, is_negative=False)
    
        # Сбрасываем состояние
        await state.clear()
    
        await message.answer(
            f"✅ Ключевые слова успешно обновлены!\n\n"
            f"<b>Новый список:</b>\n<code>{', '.join(keywords)}</code>",
            parse_mode="HTML",
            reply_markup=get_main_keyboard()  # Возвращаем главное меню
        )

    @dp.message(UserStates.waiting_for_negative)
    async def process_negative_input(message: Message, state: FSMContext):
        """Обработка ввода слов-исключений"""
        if not message.text.strip():
            await message.answer("❌ Вы отправили пустое сообщение. Пожалуйста, введите слова через запятую.")
            return
    
        # Очищаем и разбиваем ввод пользователя
        raw_negatives = [word.strip().lower() for word in message.text.split(',')]
        negatives = [word for word in raw_negatives if word]  # Убираем пустые строки
    
        # Сохраняем в базу данных
        user_id = message.from_user.id
        db.set_keywords(user_id, negatives, is_negative=True)
    
        # Сбрасываем состояние
        await state.clear()
    
        await message.answer(
            f"✅ Слова-исключения успешно обновлены!\n\n"
            f"<b>Новый список:</b>\n<code>{', '.join(negatives) if negatives else 'список пуст'}</code>",
            parse_mode="HTML",
            reply_markup=get_main_keyboard()  # Возвращаем главное меню
        )
    
    async def start_news_check(message: Message, period_hours: int, user_id: int):
        """Запуск проверки новостей"""
        # Обновляем время последней проверки
        db.update_last_check(user_id)
        
        # Получаем данные пользователя
        channels = db.get_channels(user_id)
        keywords, negative_keywords = db.get_keywords(user_id)
        
        # Если нет ключевых слов - используем значения по умолчанию
        if not keywords:
            keywords = ["технологии", "программирование", "стартап"]
            db.set_keywords(user_id, keywords, is_negative=False)
        
        period_text = format_period_text(period_hours)
        
        # Отправляем сообщение о начале проверки
        progress_msg = await message.answer(
            f"🔍 <b>Начинаю проверку...</b>\n\n"
            f"<b>Период:</b> {period_text}\n"
            f"<b>Каналов:</b> {len(channels)}\n"
            f"<b>Тегов:</b> {len(keywords)}\n"
            f"<b>Статус:</b> подключаюсь к каналам",
            parse_mode="HTML"
        )
        
        total_found = 0
        channels_processed = 0
        channels_with_news = 0
        
        # Проверяем каждый канал
        for i, channel in enumerate(channels, 1):
            try:
                # Обновляем статус
                if i % 2 == 0 or i == len(channels):  # Каждые 2 канала или последний
                    await progress_msg.edit_text(
                        f"🔍 <b>Проверяю каналы...</b>\n\n"
                        f"<b>Прогресс:</b> {i}/{len(channels)}\n"
                        f"<b>Текущий:</b> @{channel}\n"
                        f"<b>Найдено новостей:</b> {total_found}",
                        parse_mode="HTML"
                    )
                
                # Получаем сообщения из канала
                messages = await parser.get_channel_messages(channel)
                
                if not messages:
                    logging.info(f"Не удалось получить сообщения из @{channel}")
                    continue
                
                channels_processed += 1
                
                # Фильтруем по времени
                filtered_messages = parser.filter_messages_by_time(messages, period_hours)
                
                if not filtered_messages:
                    continue
                
                # Ищем релевантные новости
                channel_news_found = 0
                
                for msg in filtered_messages:
                    # Анализируем сообщение
                    analysis = analyze_message(msg['text'], keywords, negative_keywords)
                    
                    if analysis['relevant']:
                        # Генерируем хеш новости
                        news_hash = db.generate_news_hash(
                            msg['text'], 
                            channel, 
                            msg.get('id')
                        )
                        
                        # Проверяем, не отправляли ли уже
                        if not db.is_news_sent(user_id, news_hash):
                            # Отправляем новость пользователю
                            await send_news_item(
                                bot, 
                                user_id, 
                                msg, 
                                analysis['keywords'], 
                                channel
                            )
                            
                            # Отмечаем как отправленную
                            db.mark_news_sent(
                                user_id, 
                                news_hash, 
                                channel, 
                                msg.get('id')
                            )
                            
                            total_found += 1
                            channel_news_found += 1
                            
                            # Пауза между отправками
                            await asyncio.sleep(0.5)
                
                if channel_news_found > 0:
                    channels_with_news += 1
                    
            except Exception as e:
                logging.error(f"Ошибка проверки канала @{channel}: {e}")
                continue
        
        # Сохраняем историю проверки
        db.add_check_history(
            user_id, 
            period_hours, 
            channels_processed, 
            total_found, 
            success=True
        )
        
        # Формируем итоговое сообщение
        if total_found > 0:
            result_text = (
                f"✅ <b>Проверка завершена!</b>\n\n"
                f"<b>Период:</b> {period_text}\n"
                f"<b>Каналов проверено:</b> {channels_processed}/{len(channels)}\n"
                f"<b>Каналов с новостями:</b> {channels_with_news}\n"
                f"<b>Найдено новостей:</b> {total_found}\n\n"
                f"Все новости отправлены вам."
            )
        else:
            result_text = (
                f"📭 <b>Новых новостей не найдено</b>\n\n"
                f"<b>Период:</b> {period_text}\n"
                f"<b>Каналов проверено:</b> {channels_processed}/{len(channels)}\n\n"
                f"<b>Возможные причины:</b>\n"
                f"• В выбранный период не было сообщений\n"
                f"• Сообщения не содержат ваших ключевых слов\n"
                f"• Каналы могут быть приватными\n"
                f"• Попробуйте увеличить период поиска"
            )
        
        await progress_msg.edit_text(result_text, parse_mode="HTML")
        
        # Закрываем сессию парсера
        await parser.close_session()
    
    async def send_news_item(bot: Bot, user_id: int, message: Dict, 
                           found_keywords: List[str], channel: str):
        """Отправка одной новости"""
        try:
            # Обрезаем текст если слишком длинный
            news_text = message['text']
            if len(news_text) > 3500:
                news_text = news_text[:3500] + "..."
            
            # Форматируем время
            time_str = ""
            if message.get('timestamp'):
                time_str = message['timestamp'].strftime("%d.%m.%Y %H:%M")
            
            # Формируем сообщение
            message_text = f"📰 <b>@{channel}</b>\n\n"
            
            if time_str:
                message_text += f"<i>📅 {time_str}</i>\n\n"
            
            message_text += f"{news_text}\n\n"
            
            if found_keywords:
                message_text += f"🔍 <b>Найдены теги:</b> {', '.join(found_keywords[:3])}\n"
            
            if message.get('url'):
                message_text += f"\n🔗 <a href='{message['url']}'>Читать в канале</a>"
            
            await bot.send_message(
                chat_id=user_id,
                text=message_text,
                parse_mode="HTML",
                disable_web_page_preview=False
            )
            
        except Exception as e:
            logging.error(f"Ошибка отправки новости: {e}")
    
    @dp.callback_query(F.data.startswith("remove_channel:"))
    async def callback_remove_channel(callback: types.CallbackQuery):
        """Удаление канала через кнопку"""
        try:
            channel = callback.data.split(":")[1]
            user_id = callback.from_user.id
            
            # Удаляем канал из базы
            if db.remove_channel(user_id, channel):
                # Обновляем сообщение
                await callback.message.edit_text(
                    f"✅ Канал @{channel} удален\n\n"
                    f"Остальные каналы остались без изменений.\n\n"
                    f"Чтобы добавить новый канал, нажмите '➕ Добавить канал'",
                    parse_mode="HTML"
                )
            else:
                await callback.message.edit_text(
                    f"❌ Канал @{channel} не найден или уже удален",
                    parse_mode="HTML"
                )
            
        except Exception as e:
            logging.error(f"Ошибка удаления канала: {e}")
            await callback.message.edit_text("❌ Ошибка при удалении канала")
        
        await callback.answer()
    
    @dp.message(F.text == "📊 Статистика")
    async def cmd_stats(message: Message):
        """Статистика пользователя"""
        user_id = message.from_user.id
        stats = db.get_user_stats(user_id)
        channels = db.get_channels(user_id)
        keywords, negative = db.get_keywords(user_id)
        
        stats_text = f"📊 <b>Ваша статистика</b>\n\n"
        stats_text += f"<b>Каналов:</b> {stats['channels']}\n"
        stats_text += f"<b>Ключевых слов:</b> {stats['keywords']}\n"
        stats_text += f"<b>Исключений:</b> {stats['negative']}\n"
        stats_text += f"<b>Получено новостей:</b> {stats['news_received']}\n\n"
        
        if stats['last_check']:
            check_time, period_hours, news_found = stats['last_check']
            check_time_str = datetime.strptime(check_time, "%Y-%m-%d %H:%M:%S").strftime("%d.%m.%Y %H:%M")
            period_text = format_period_text(period_hours)
            stats_text += f"<b>Последняя проверка:</b>\n"
            stats_text += f"• Время: {check_time_str}\n"
            stats_text += f"• Период: {period_text}\n"
            stats_text += f"• Найдено: {news_found} нов.\n\n"
        
        if channels:
            stats_text += f"<b>Ваши каналы:</b>\n"
            for i, channel in enumerate(channels[:5], 1):
                stats_text += f"{i}. @{channel}\n"
            if len(channels) > 5:
                stats_text += f"... и еще {len(channels) - 5}\n"
        
        await message.answer(stats_text, parse_mode="HTML")
    
    @dp.message(F.text == "📢 Мои каналы")
    async def cmd_my_channels(message: Message):
        """Показать каналы пользователя"""
        user_id = message.from_user.id
        channels = db.get_channels(user_id)
        
        if not channels:
            await message.answer(
                "📭 <b>У вас нет каналов</b>\n\n"
                "Добавьте каналы через кнопку '➕ Добавить канал'\n\n"
                "<b>Популярные IT каналы:</b>\n"
                "@ru_tech, @tproger, @vcnews, @ainewsru",
                parse_mode="HTML"
            )
            return
        
        # Создаем кнопки для каналов
        buttons = []
        for channel in channels[:8]:  # Ограничиваем 8 каналами
            buttons.append([
                InlineKeyboardButton(
                    text=f"📢 @{channel}",
                    url=f"https://t.me/{channel}"
                ),
                InlineKeyboardButton(
                    text="🗑️ Удалить",
                    callback_data=f"remove_channel:{channel}"
                )
            ])
        
        # Кнопка для проверки этих каналов
        buttons.append([
            InlineKeyboardButton(
                text="🔍 Проверить эти каналы",
                callback_data="check_my_channels"
            )
        ])
        
        await message.answer(
            f"📢 <b>Ваши каналы</b> ({len(channels)})\n\n"
            f"Нажмите на название для перехода\n"
            f"Или удалите ненужные:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
        )
    
    @dp.callback_query(F.data == "check_my_channels")
    async def callback_check_my_channels(callback: types.CallbackQuery):
        """Проверка текущих каналов"""
        await callback.message.edit_text(
            "🔍 <b>За какой период проверять эти каналы?</b>",
            parse_mode="HTML",
            reply_markup=get_period_keyboard()
        )
        await callback.answer()
    
    @dp.message(F.text == "🏷️ Мои теги")
    async def cmd_my_tags(message: Message):
        """Показать и настроить теги"""
        user_id = message.from_user.id
        keywords, negative = db.get_keywords(user_id)
        
        # Если нет тегов - значения по умолчанию
        if not keywords:
            keywords = ["технологии", "программирование", "стартап"]
        
        keywords_text = ", ".join(keywords) if keywords else "не заданы"
        negative_text = ", ".join(negative) if negative else "не заданы"
        
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✏️ Изменить теги", callback_data="edit_keywords"),
                InlineKeyboardButton(text="🚫 Изменить исключения", callback_data="edit_negative")
            ],
            [
                InlineKeyboardButton(text="🔄 По умолчанию", callback_data="reset_tags_default")
            ]
        ])
        
        await message.answer(
            f"🏷️ <b>Ваши теги и фильтры</b>\n\n"
            f"<b>🔍 Ключевые слова:</b>\n"
            f"<code>{keywords_text}</code>\n\n"
            f"<b>🚫 Слова-исключения:</b>\n"
            f"<code>{negative_text}</code>\n\n"
            f"<b>Как работает:</b>\n"
            f"1. Бот ищет сообщения с ключевыми словами\n"
            f"2. Игнорирует сообщения с исключениями\n"
            f"3. Отправляет вам только релевантное",
            parse_mode="HTML",
            reply_markup=keyboard
        )
    
    @dp.callback_query(F.data == "edit_keywords")
    async def callback_edit_keywords(callback: types.CallbackQuery, state: FSMContext):
        """Редактирование ключевых слов"""
        await callback.message.answer(
            "✏️ <b>Введите новые ключевые слова:</b>\n\n"
            "<b>Формат:</b> слова через запятую\n"
            "<b>Пример:</b> технологии, программирование, стартап\n\n"
            "Текущие теги будут заменены.",
            parse_mode="HTML"
        )
        await state.set_state(UserStates.waiting_for_keywords)
        await callback.answer()

    @dp.callback_query(F.data == "edit_negative")
    async def callback_edit_negative(callback: types.CallbackQuery, state: FSMContext):
        """Редактирование исключений"""
        await callback.message.answer(
            "🚫 <b>Введите слова-исключения:</b>\n\n"
            "<b>Формат:</b> слова через запятую\n"
            "<b>Пример:</b> смерть, авария, преступление\n\n"
            "Текущие исключения будут заменены.",
            parse_mode="HTML"
        )
        await state.set_state(UserStates.waiting_for_negative)
        await callback.answer()

    @dp.callback_query(F.data == "reset_tags_default")
    async def callback_reset_tags_default(callback: types.CallbackQuery):
        """Сброс тегов к значениям по умолчанию"""
        user_id = callback.from_user.id
        
        # Устанавливаем значения по умолчанию
        default_keywords = ["технологии", "программирование", "стартап", "инвестиции"]
        default_negative = ["смерть", "авария", "преступление", "война"]
        
        db.set_keywords(user_id, default_keywords, is_negative=False)
        db.set_keywords(user_id, default_negative, is_negative=True)
        
        await callback.message.edit_text(
            f"🔄 <b>Теги сброшены к значениям по умолчанию</b>\n\n"
            f"<b>Ключевые слова:</b>\n"
            f"<code>{', '.join(default_keywords)}</code>\n\n"
            f"<b>Исключения:</b>\n"
            f"<code>{', '.join(default_negative)}</code>",
            parse_mode="HTML"
        )
        await callback.answer()
    
    @dp.message(F.text == "➕ Добавить канал")
    async def cmd_add_channel(message: Message):
        """Добавление нового канала"""
        await message.answer(
            "➕ <b>Добавление канала</b>\n\n"
            "Отправьте username канала:\n"
            "<code>@username</code>\n\n"
            "<b>Или выберите из популярных:</b>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [
                    InlineKeyboardButton(text="@ru_tech", callback_data="quick_add:ru_tech"),
                    InlineKeyboardButton(text="@tproger", callback_data="quick_add:tproger")
                ],
                [
                    InlineKeyboardButton(text="@vcnews", callback_data="quick_add:vcnews"),
                    InlineKeyboardButton(text="@ainewsru", callback_data="quick_add:ainewsru")
                ],
                [
                    InlineKeyboardButton(text="@roem", callback_data="quick_add:roem"),
                    InlineKeyboardButton(text="@digital", callback_data="quick_add:digital")
                ]
            ])
        )
    
    @dp.callback_query(F.data.startswith("quick_add:"))
    async def callback_quick_add(callback: types.CallbackQuery):
        """Быстрое добавление канала"""
        channel = callback.data.split(":")[1]
        user_id = callback.from_user.id
        
        if db.add_channel(user_id, f"@{channel}"):
            await callback.message.edit_text(
                f"✅ Канал @{channel} добавлен!\n\n"
                f"Теперь настройте теги и проверьте новости.",
                parse_mode="HTML"
            )
        else:
            await callback.message.edit_text(f"ℹ️ Канал @{channel} уже добавлен")
        
        await callback.answer()
    
    @dp.message(F.text.startswith("@"))
    async def handle_direct_channel_input(message: Message):
        """Обработка прямого ввода @канала"""
        channel = message.text
        user_id = message.from_user.id
        
        if db.add_channel(user_id, channel):
            await message.answer(
                f"✅ Канал {channel} добавлен!\n\n"
                f"Теперь настройте теги через '🏷️ Мои теги'\n"
                f"И проверьте новости через '🔍 Проверить новости'",
                parse_mode="HTML"
            )
        else:
            await message.answer(f"ℹ️ Канал {channel} уже добавлен")
    
    @dp.message(F.text == "⚙️ Настройки")
    async def cmd_settings(message: Message):
        """Настройки бота"""
        settings_text = (
            "⚙️ <b>Настройки бота</b>\n\n"
            "<b>Текущий режим:</b> Telegram Web парсинг\n"
            "<b>Ограничения:</b>\n"
            "• Только публичные каналы\n"
            "• До ~50 последних сообщений\n"
            "• Задержка между запросами\n\n"
            "<b>Рекомендации:</b>\n"
            "• Добавляйте публичные каналы\n"
            "• Используйте точные ключевые слова\n"
            "• Проверяйте новости раз в несколько часов"
        )
        
        await message.answer(settings_text, parse_mode="HTML")
    
    @dp.message(Command("help"))
    async def cmd_help(message: Message):
        """Справка по боту"""
        help_text = (
            "<b>📖 Помощь по боту</b>\n\n"
            
            "<b>🎯 Основной процесс:</b>\n"
            "1. Добавьте каналы (кнопка '➕ Добавить канал')\n"
            "2. Настройте теги (кнопка '🏷️ Мои теги')\n"
            "3. Проверьте новости (кнопка '🔍 Проверить новости')\n"
            "4. Выберите период проверки\n\n"
            
            "<b>🔍 Как работает проверка:</b>\n"
            "1. Бот получает сообщения из каналов\n"
            "2. Фильтрует по выбранному периоду\n"
            "3. Ищет ваши ключевые слова\n"
            "4. Исключает сообщения с запрещенными словами\n"
            "5. Отправляет вам уникальные новости\n\n"
            
            "<b>🏷️ Пример тегов:</b>\n"
            "• Ключевые: технологии, программирование, стартап\n"
            "• Исключения: смерть, авария, преступление\n\n"
            
            "<b>📅 Доступные периоды:</b>\n"
            "• 1-24 часа\n"
            "• 1-7 дней\n"
            "• Вся история (все доступные сообщения)\n\n"
            
            "<b>⚠️ Ограничения:</b>\n"
            "• Только публичные каналы\n"
            "• Не все каналы доступны через web\n"
            "• Ограниченная история сообщений"
        )
        
        await message.answer(help_text, parse_mode="HTML")
    
    @dp.message(Command("test_channel"))
    async def cmd_test_channel(message: Message, command: CommandObject):
        """Тестовая команда для проверки канала"""
        if not command.args:
            await message.answer("Укажите канал: /test_channel @username")
            return
        
        channel = command.args.strip().lstrip('@')
        await message.answer(f"🔍 Тестирую канал @{channel}...")
        
        try:
            messages = await parser.get_channel_messages(channel, limit=10)
            
            if messages:
                result = f"✅ Канал @{channel} доступен\n\n"
                result += f"Найдено сообщений: {len(messages)}\n"
                result += f"Последнее сообщение:\n"
                result += f"• Время: {messages[0].get('timestamp', 'неизвестно')}\n"
                result += f"• Длина: {len(messages[0]['text'])} символов\n"
                result += f"• ID: {messages[0].get('id', 'нет')}\n\n"
                result += "Можно добавить этот канал!"
            else:
                result = f"❌ Канал @{channel} не доступен\n\n"
                result += "Возможные причины:\n"
                result += "• Канал приватный\n"
                result += "• Ошибка подключения\n"
                result += "• Канал не существует"
            
            await message.answer(result)
            
        except Exception as e:
            await message.answer(f"❌ Ошибка: {str(e)[:100]}")
        
        finally:
            await parser.close_session()
    
    # ==================== ЗАПУСК БОТА ====================
    
    logger.info("✅ Бот запущен с Telegram Web парсингом!")
    
    try:
        await dp.start_polling(bot)
    finally:
        # Закрываем соединения при остановке
        await parser.close_session()

if __name__ == "__main__":
    asyncio.run(main())