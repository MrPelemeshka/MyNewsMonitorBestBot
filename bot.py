import asyncio
import logging
import re
import html
import random
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Tuple
from collections import defaultdict

from aiogram import Bot, Dispatcher, types, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    Message, InlineKeyboardMarkup, InlineKeyboardButton, 
    ReplyKeyboardMarkup, KeyboardButton, CallbackQuery,
    ReplyKeyboardRemove
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.enums import ParseMode

from config import config
from database import db
from parser import parser

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bot.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Создаем роутер
router = Router()

# Состояния для FSM
class UserStates(StatesGroup):
    waiting_for_keywords = State()
    waiting_for_negative = State()
    waiting_for_channel = State()
    waiting_for_weighted_keywords = State()
    waiting_for_category = State()

# Вспомогательные функции
def escape_html(text: str) -> str:
    """Экранирование HTML символов с фильтрацией неподдерживаемых тегов"""
    # Сначала экранируем все
    text = html.escape(text)
    
    # Удаляем неподдерживаемые теги
    unsupported_tags = ['<small>', '</small>', '<big>', '</big>', '<center>', '</center>']
    for tag in unsupported_tags:
        text = text.replace(tag, '')
    
    return text

# Класс для форматирования новостей
class NewsFormatter:
    """Улучшенный форматировщик новостей"""
    
    @staticmethod
    def _extract_title(text: str, max_length: int = 100) -> str:
        """Извлечение заголовка из текста"""
        # Берем первую строку или первые N символов
        lines = text.strip().split('\n')
        first_line = lines[0].strip()
        
        if len(first_line) > 10 and len(first_line) < max_length:
            return first_line
        
        # Или обрезаем начало текста
        return text[:max_length].strip() + ('...' if len(text) > max_length else '')
    
    @staticmethod
    def _create_excerpt(text: str, max_length: int = 300) -> str:
        """Создание краткого описания"""
        text = re.sub(r'\s+', ' ', text.strip())
        
        if len(text) <= max_length:
            return text
        
        # Обрезаем до последнего полного предложения или слова
        if '.' in text[:max_length]:
            cut_point = text[:max_length].rfind('.') + 1
        elif ' ' in text[:max_length]:
            cut_point = text[:max_length].rfind(' ') + 1
        else:
            cut_point = max_length
        
        return text[:cut_point].strip() + '...'
    
    @staticmethod
    def _determine_category(keywords: List[str]) -> str:
        """Определение категории по ключевым словам"""
        for category, terms in config.CATEGORIES.items():
            for keyword in keywords:
                keyword_lower = keyword.lower()
                for term in terms:
                    if term in keyword_lower or keyword_lower in term:
                        return category
        return 'other'
    
    @staticmethod
    def format_news_card(msg: Dict, analysis: Dict, category: str = None) -> str:
        """Форматирование новости в виде карточки"""
        if not category:
            found_keywords = analysis.get('found_keywords', [])
            keywords = [k['keyword'] for k in found_keywords] if isinstance(found_keywords, list) else []
            category = NewsFormatter._determine_category(keywords)
        
        icon = config.CATEGORY_ICONS.get(category, '📰')
        title = NewsFormatter._extract_title(msg['text'])
        excerpt = NewsFormatter._create_excerpt(msg['text'], 250)
        channel = msg.get('channel', 'unknown')
        
        # Форматируем время
        time_str = ""
        if msg.get('timestamp'):
            now = datetime.now()
            msg_time = msg['timestamp']
            
            if now.date() == msg_time.date():
                time_str = f"Сегодня в {msg_time.strftime('%H:%M')}"
            elif (now - timedelta(days=1)).date() == msg_time.date():
                time_str = f"Вчера в {msg_time.strftime('%H:%M')}"
            else:
                time_str = msg_time.strftime("%d.%m.%Y в %H:%M")
        
        # Собираем HTML сообщение
        parts = []
        
        # Заголовок с иконкой категории
        parts.append(f"{icon} <b>{escape_html(title)}</b>\n")
        
        # Источник и время
        parts.append(f"📢 @{channel}  ⏰ {time_str}\n")
        
        # Рейтинг релевантности (если есть)
        if 'score' in analysis:
            score = analysis['score']
            if score > 3:
                stars = min(int(score / 2), 5)  # Максимум 5 звезд
                parts.append("⭐" * stars + "\n")
        
        # Основной текст
        parts.append(f"\n{escape_html(excerpt)}\n")
        
        # Ключевые слова
        found_keywords = analysis.get('found_keywords', [])
        if found_keywords and isinstance(found_keywords, list):
            keywords = [k['keyword'] for k in found_keywords[:3]]
            keywords_text = ", ".join(keywords)
            parts.append(f"\n🏷️ <i>{escape_html(keywords_text)}</i>\n")
        
        # Дополнительная информация
        if msg.get('has_media'):
            parts.append("📎 <i>Есть вложения</i>\n")
        
        if msg.get('views'):
            parts.append(f"👁️ <i>{msg['views']} просмотров</i>\n")
        
        # Ссылка
        if msg.get('url'):
            parts.append(f"\n🔗 <a href='{escape_html(msg['url'])}'>Читать полностью в канале</a>")
        
        return "".join(parts)

# Класс для анализа релевантности
class RelevanceAnalyzer:
    """Анализатор релевантности с весовыми коэффициентами"""
    
    @staticmethod
    def parse_weighted_keywords(keywords_input: str) -> List[Tuple[str, float]]:
        """Парсинг ключевых слов с весами"""
        weighted_keywords = []
        
        for item in keywords_input.split(','):
            item = item.strip()
            if not item:
                continue
                
            if ':' in item:
                parts = item.split(':')
                if len(parts) == 2:
                    keyword = parts[0].strip()
                    try:
                        weight = float(parts[1].strip())
                        weighted_keywords.append((keyword, max(0.1, min(weight, 5.0))))
                    except ValueError:
                        weighted_keywords.append((keyword, 1.0))
            else:
                weighted_keywords.append((item, 1.0))
        
        return weighted_keywords
    
    @staticmethod
    def analyze_message(text: str, weighted_keywords: List[Tuple[str, float]], 
                       negative_keywords: List[str]) -> Dict:
        """Анализ сообщения с учетом весов"""
        text_lower = f" {text.lower()} "
        
        # Поиск ключевых слов с весами
        found_keywords = []
        total_score = 0
        
        for keyword, weight in weighted_keywords:
            keyword_lower = keyword.lower()
            
            # Разные стратегии поиска с разными коэффициентами
            score = 0
            
            # Точное совпадение слова (лучший результат)
            if f" {keyword_lower} " in text_lower:
                score = weight * 2.0
            
            # Часть слова или с другими символами
            elif keyword_lower in text_lower:
                # Проверяем, чтобы это было отдельное слово
                pattern = r'[^a-zA-Zа-яА-Я0-9]' + re.escape(keyword_lower) + r'[^a-zA-Zа-яА-Я0-9]'
                if re.search(pattern, text_lower):
                    score = weight * 1.5
                else:
                    score = weight * 1.0
            
            if score > 0:
                found_keywords.append({
                    'keyword': keyword,
                    'weight': weight,
                    'score': score
                })
                total_score += score
        
        # Проверка отрицательных ключевых слов
        negative_score = 0
        found_negative = []
        
        for neg_keyword in negative_keywords:
            neg_lower = neg_keyword.lower()
            if f" {neg_lower} " in text_lower:
                negative_score += 3.0
                found_negative.append(neg_keyword)
            elif neg_lower in text_lower:
                negative_score += 1.5
                found_negative.append(neg_keyword)
        
        # Итоговый скор с учетом отрицательных слов
        final_score = max(0, total_score - negative_score)
        
        return {
            'relevant': final_score > 0.5,
            'score': final_score,
            'total_score': total_score,
            'negative_score': negative_score,
            'found_keywords': found_keywords,
            'found_negative': found_negative,
            'keyword_count': len(found_keywords),
            'has_negative': negative_score > 0
        }

# Класс для управления очередью отправки
class NewsQueueManager:
    """Менеджер очереди отправки новостей"""
    
    def __init__(self, bot: Bot = None):
        self.queue = asyncio.Queue()
        self.processing = False
        self.stats = {
            'sent': 0,
            'failed': 0,
            'skipped': 0,
            'queue_size': 0
        }
        self.bot = bot
    
    def set_bot(self, bot: Bot):
        """Установить бота"""
        self.bot = bot
    
    async def add_news_batch(self, user_id: int, news_items: List[Dict]):
        """Добавление партии новостей в очередь с сортировкой"""
        if not news_items:
            return
        
        # Сортируем по релевантности (самые релевантные сначала)
        sorted_items = sorted(news_items, 
                            key=lambda x: x.get('analysis', {}).get('score', 0), 
                            reverse=True)
        
        for item in sorted_items:
            await self.queue.put({
                'user_id': user_id,
                'news_item': item,
                'added_at': datetime.now()
            })
        
        self.stats['queue_size'] = self.queue.qsize()
    
    async def process_queue(self, batch_size: int = 5, delay: float = 1.0):
        """Обработка очереди с ограничением скорости"""
        self.processing = True
        
        while self.processing:
            batch = []
            try:
                # Собираем батч
                for _ in range(min(batch_size, self.queue.qsize())):
                    if not self.queue.empty():
                        item = await self.queue.get()
                        batch.append(item)
                    else:
                        break
                
                if batch and self.bot:
                    # Отправляем батч
                    sent_count = await self._send_batch(batch)
                    self.stats['sent'] += sent_count
                    self.stats['queue_size'] = self.queue.qsize()
                    
                    # Пауза между батчами
                    if sent_count > 0:
                        await asyncio.sleep(delay)
                else:
                    # Очередь пуста, ждем
                    await asyncio.sleep(5)
                    
            except Exception as e:
                logger.error(f"Ошибка обработки очереди: {e}")
                self.stats['failed'] += len(batch)
                await asyncio.sleep(10)
    
    async def _send_batch(self, batch: List[Dict]) -> int:
        """Отправка батча новостей"""
        sent_count = 0
        
        for item in batch:
            try:
                user_id = item['user_id']
                news_item = item['news_item']
                
                # Форматируем сообщение
                found_keywords = news_item['analysis'].get('found_keywords', [])
                keywords = [k['keyword'] for k in found_keywords] if isinstance(found_keywords, list) else []
                category = NewsFormatter._determine_category(keywords)
                
                message_text = NewsFormatter.format_news_card(
                    news_item['message'],
                    news_item['analysis'],
                    category
                )
                
                # Отправляем сообщение
                await self.bot.send_message(
                    chat_id=user_id,
                    text=message_text,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=False
                )
                
                # Отмечаем как отправленное
                db.mark_news_sent(
                    user_id, 
                    news_item['hash'], 
                    news_item['message']['channel'],
                    news_item['message'].get('id')
                )
                
                sent_count += 1
                
                # Небольшая пауза между сообщениями в батче
                await asyncio.sleep(0.1)
                
            except Exception as e:
                logger.error(f"Ошибка отправки новости: {e}")
                continue
        
        return sent_count
    
    def stop_processing(self):
        """Остановка обработки очереди"""
        self.processing = False
    
    def get_stats(self) -> Dict:
        """Получение статистики очереди"""
        return {
            **self.stats,
            'processing': self.processing
        }

# Класс для аналитики
class UserAnalytics:
    """Аналитика пользовательской активности"""
    
    def __init__(self, database):
        self.db = database
    
    async def get_detailed_stats(self, user_id: int) -> Dict:
        """Подробная статистика пользователя"""
        basic_stats = db.get_user_stats(user_id)
        channels = db.get_channels(user_id)
        keywords, negative = db.get_keywords(user_id)
        
        # Анализ категорий интересов
        categories = defaultdict(int)
        for keyword in keywords:
            category = NewsFormatter._determine_category([keyword])
            categories[category] += 1
        
        # Форматируем категории
        formatted_categories = []
        for category, count in categories.items():
            icon = config.CATEGORY_ICONS.get(category, '📝')
            formatted_categories.append(f"{icon} {category}: {count}")
        
        return {
            'basic': basic_stats,
            'channels_count': len(channels),
            'keywords_count': len(keywords),
            'negative_count': len(negative),
            'categories': formatted_categories,
            'categories_raw': dict(categories),
            'top_categories': sorted(categories.items(), key=lambda x: x[1], reverse=True)[:3]
        }

# Глобальные объекты
news_queue = None
analytics = UserAnalytics(db)

# Клавиатуры
def get_main_keyboard() -> ReplyKeyboardMarkup:
    """Основная клавиатура с улучшенным дизайном"""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🔍 Проверить новости"), KeyboardButton(text="📊 Статистика")],
            [KeyboardButton(text="📢 Мои каналы"), KeyboardButton(text="🏷️ Мои теги")],
            [KeyboardButton(text="➕ Добавить канал"), KeyboardButton(text="⚙️ Настройки")],
            [KeyboardButton(text="📈 Аналитика"), KeyboardButton(text="❓ Помощь")]
        ],
        resize_keyboard=True,
        input_field_placeholder="Выберите действие...",
        selective=True
    )

def get_channels_keyboard(channels: List[str]) -> InlineKeyboardMarkup:
    """Клавиатура для управления каналами с пагинацией"""
    builder = InlineKeyboardBuilder()
    
    for i, channel in enumerate(channels[:10], 1):
        builder.button(
            text=f"{i}. ❌ @{channel}",
            callback_data=f"remove_channel:{channel}"
        )
    
    builder.adjust(2)  # 2 кнопки в ряд
    
    # Кнопки действий
    builder.row(
        InlineKeyboardButton(text="📥 Добавить еще", callback_data="add_more_channels"),
        InlineKeyboardButton(text="🔄 Проверить все", callback_data="check_all_channels")
    )
    
    return builder.as_markup()

def get_settings_keyboard() -> InlineKeyboardMarkup:
    """Улучшенная клавиатура настроек"""
    builder = InlineKeyboardBuilder()
    
    builder.button(text="✏️ Изменить теги", callback_data="edit_keywords")
    builder.button(text="⚖️ Весовые теги", callback_data="edit_weighted_keywords")
    builder.button(text="🚫 Исключения", callback_data="edit_negative")
    builder.button(text="📁 Категории", callback_data="manage_categories")
    builder.button(text="❓ Как работает поиск", callback_data="how_it_works")
    builder.button(text="📊 Статистика парсера", callback_data="parser_stats")
    
    builder.adjust(2, 2, 1, 1)  # Распределение по рядам
    
    return builder.as_markup()

def get_analytics_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура аналитики"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📅 Активность", callback_data="analytics_activity"),
            InlineKeyboardButton(text="🏷️ Категории", callback_data="analytics_categories")
        ],
        [
            InlineKeyboardButton(text="📢 Топ каналы", callback_data="analytics_top_channels"),
            InlineKeyboardButton(text="🎯 Рекомендации", callback_data="analytics_recommendations")
        ]
    ])

# ==================== КОМАНДЫ ====================

@router.message(Command("start"))
async def cmd_start(message: Message):
    """Команда /start"""
    user_id = message.from_user.id
    db.add_user(user_id, message.from_user.username, message.from_user.first_name)
    
    welcome_text = config.WELCOME_MESSAGE.format(name=message.from_user.first_name)
    
    await message.answer(
        welcome_text, 
        parse_mode=ParseMode.HTML, 
        reply_markup=get_main_keyboard(),
        disable_notification=True
    )
    
    # Предлагаем начать с добавления канала
    channels = db.get_channels(user_id)
    if not channels:
        await message.answer(
            "🎯 <b>Быстрый старт:</b>\n\n"
            "1. Отправьте @username канала\n"
            "2. Настройте теги\n"
            "3. Проверьте новости\n\n"
            "<i>Пример канала:</i> <code>@tproger</code>",
            parse_mode=ParseMode.HTML
        )

@router.message(Command("help"))
async def cmd_help(message: Message):
    """Команда /help"""
    await message.answer(config.HELP_MESSAGE, parse_mode=ParseMode.HTML)

@router.message(Command("analytics"))
async def cmd_analytics(message: Message):
    """Команда /analytics - детальная аналитика"""
    user_id = message.from_user.id
    stats = await analytics.get_detailed_stats(user_id)
    
    analytics_text = "📈 <b>Детальная аналитика</b>\n\n"
    
    # Основная статистика
    analytics_text += f"<b>Основные показатели:</b>\n"
    analytics_text += f"• 📢 Каналов: {stats['basic']['channels']}\n"
    analytics_text += f"• 🏷️ Тегов: {stats['keywords_count']}\n"
    analytics_text += f"• 🚫 Исключений: {stats['negative_count']}\n"
    analytics_text += f"• 📨 Отправлено новостей: {stats['basic']['sent_news']}\n\n"
    
    # Категории интересов
    if stats['categories']:
        analytics_text += f"<b>Ваши интересы по категориям:</b>\n"
        for category in stats['categories']:
            analytics_text += f"• {category}\n"
    
    await message.answer(
        analytics_text,
        parse_mode=ParseMode.HTML,
        reply_markup=get_analytics_keyboard()
    )

@router.message(Command("admin"))
async def cmd_admin(message: Message):
    """Панель администратора"""
    if message.from_user.id != config.ADMIN_ID:
        await message.answer("⛔ Доступ запрещен")
        return
    
    admin_text = "⚙️ <b>Панель администратора</b>\n\n"
    
    # Статистика системы
    parser_stats = parser.get_stats()
    
    admin_text += f"<b>Статистика парсера:</b>\n"
    admin_text += f"• Успешных запросов: {parser_stats.get('success', 0)}\n"
    admin_text += f"• Ошибок: {parser_stats.get('failures', 0)}\n"
    admin_text += f"• Таймаутов: {parser_stats.get('timeouts', 0)}\n"
    
    cache_stats = parser_stats.get('cache_stats', {})
    admin_text += f"• Хит-рейт кэша: {cache_stats.get('hit_rate', 0):.1%}\n\n"
    
    # Статистика очереди
    global news_queue
    if news_queue:
        queue_stats = news_queue.get_stats()
        admin_text += f"<b>Очередь отправки:</b>\n"
        admin_text += f"• Отправлено: {queue_stats.get('sent', 0)}\n"
        admin_text += f"• В очереди: {queue_stats.get('queue_size', 0)}\n"
        admin_text += f"• Ошибок: {queue_stats.get('failed', 0)}\n"
    
    await message.answer(admin_text, parse_mode=ParseMode.HTML)

@router.message(Command("channels"))
async def cmd_channels(message: Message):
    """Команда /channels - список каналов"""
    user_id = message.from_user.id
    channels = db.get_channels(user_id)
    
    if not channels:
        await message.answer(
            "📭 <b>У вас еще нет каналов</b>\n\n"
            "Добавьте каналы одним из способов:\n"
            "1. Через кнопку «➕ Добавить канал»\n"
            "2. Отправьте @username канала\n"
            "3. Пример: <code>@tproger</code>",
            parse_mode=ParseMode.HTML
        )
        return
    
    channels_text = "📢 <b>Ваши каналы:</b>\n\n"
    for i, channel in enumerate(channels, 1):
        channels_text += f"{i}. @{channel}\n"
    
    channels_text += f"\n<b>Всего:</b> {len(channels)} каналов"
    
    await message.answer(
        channels_text, 
        parse_mode=ParseMode.HTML,
        reply_markup=get_channels_keyboard(channels)
    )

@router.message(Command("tags"))
async def cmd_tags(message: Message):
    """Команда /tags - показать теги"""
    user_id = message.from_user.id
    keywords, negative = db.get_keywords(user_id)
    
    # Используем теги по умолчанию если нет своих
    if not keywords:
        keywords = config.DEFAULT_KEYWORDS
    
    keywords_text = ", ".join(keywords) if keywords else "не заданы"
    negative_text = ", ".join(negative) if negative else "не заданы"
    
    await message.answer(
        f"🏷️ <b>Ваши теги и фильтры</b>\n\n"
        f"<b>🔍 Ключевые слова для поиска:</b>\n"
        f"<code>{escape_html(keywords_text)}</code>\n\n"
        f"<b>🚫 Слова-исключения:</b>\n"
        f"<code>{escape_html(negative_text)}</code>\n\n"
        f"<i>Бот будет искать сообщения с ключевыми словами,\n"
        f"но без слов-исключений.</i>",
        parse_mode=ParseMode.HTML,
        reply_markup=get_settings_keyboard()
    )

@router.message(Command("stats"))
async def cmd_stats_command(message: Message):
    """Команда /stats - статистика"""
    user_id = message.from_user.id
    stats = db.get_user_stats(user_id)
    channels = db.get_channels(user_id)
    
    stats_text = f"📊 <b>Ваша статистика</b>\n\n"
    stats_text += f"<b>📢 Каналов:</b> {stats['channels']}\n"
    stats_text += f"<b>🏷️ Ключевых слов:</b> {stats['keywords']}\n"
    stats_text += f"<b>🚫 Исключений:</b> {stats['negative_keywords']}\n"
    stats_text += f"<b>📨 Отправлено новостей:</b> {stats['sent_news']}\n\n"
    
    if channels:
        stats_text += f"<b>Последние каналы:</b>\n"
        for i, channel in enumerate(channels[:5], 1):
            stats_text += f"{i}. @{channel}\n"
    
    await message.answer(stats_text, parse_mode=ParseMode.HTML)

# ==================== КНОПКИ ====================

@router.message(F.text == "📈 Аналитика")
async def cmd_analytics_button(message: Message):
    """Кнопка аналитики"""
    await cmd_analytics(message)

@router.message(F.text == "🔍 Проверить новости")
async def cmd_check_news(message: Message):
    """Улучшенная проверка новостей"""
    user_id = message.from_user.id
    channels = db.get_channels(user_id)
    
    if not channels:
        await message.answer(
            "❌ <b>У вас нет каналов для проверки</b>\n\n"
            "Добавьте хотя бы один канал через\n"
            "кнопку «➕ Добавить канал»",
            parse_mode=ParseMode.HTML
        )
        return
    
    keywords, negative = db.get_keywords(user_id)
    if not keywords:
        keywords = config.DEFAULT_KEYWORDS
    
    # Создаем взвешенные ключевые слова
    weighted_keywords = [(kw, 1.0) for kw in keywords]
    
    # Статус начала проверки
    status_msg = await message.answer(
        f"🔍 <b>Начинаю умную проверку...</b>\n\n"
        f"<b>Каналов:</b> {len(channels)}\n"
        f"<b>Ключевых слов:</b> {len(keywords)}\n"
        f"<b>Исключений:</b> {len(negative)}\n\n"
        f"<i>Используется улучшенный алгоритм поиска...</i>",
        parse_mode=ParseMode.HTML
    )
    
    total_found = 0
    found_by_channel = {}
    
    # Проверяем каждый канал
    for i, channel in enumerate(channels, 1):
        try:
            # Получаем только свежие сообщения (за последние 24 часа)
            messages = await parser.get_fresh_messages(channel, hours=24, limit=20)
            
            channel_news = []
            
            for msg in messages:
                # Анализируем с улучшенным алгоритмом
                analysis = RelevanceAnalyzer.analyze_message(
                    msg['text'],
                    weighted_keywords,
                    negative
                )
                
                if analysis['relevant'] and not analysis['has_negative']:
                    news_hash = db.generate_news_hash(msg['text'], channel, msg.get('id'))
                    
                    if not db.is_news_sent(user_id, news_hash):
                        channel_news.append({
                            'message': msg,
                            'analysis': analysis,
                            'hash': news_hash
                        })
            
            if channel_news:
                # Добавляем в очередь отправки
                global news_queue
                if news_queue:
                    await news_queue.add_news_batch(user_id, channel_news)
                found_by_channel[channel] = len(channel_news)
                total_found += len(channel_news)
            
            # Обновляем статус каждые 3 канала
            if i % 3 == 0 or i == len(channels):
                progress_text = (
                    f"🔍 <b>Проверяю...</b>\n\n"
                    f"<b>Прогресс:</b> {i}/{len(channels)}\n"
                    f"<b>Найдено новостей:</b> {total_found}\n"
                )
                
                if found_by_channel:
                    progress_text += f"<b>Каналы с новостями:</b> {len(found_by_channel)}"
                
                try:
                    await status_msg.edit_text(progress_text, parse_mode=ParseMode.HTML)
                except:
                    pass
            
            # Небольшая пауза между каналами
            await asyncio.sleep(0.5)
            
        except Exception as e:
            logger.error(f"Ошибка проверки канала @{channel}: {e}")
            continue
    
    # Итоговый результат
    if total_found > 0:
        result_text = (
            f"✅ <b>Проверка завершена!</b>\n\n"
            f"<b>Найдено новых сообщений:</b> {total_found}\n"
            f"<b>Каналов с новостей:</b> {len(found_by_channel)}\n"
            f"<b>Всего проверено:</b> {len(channels)}\n\n"
        )
        
        # Показываем топ каналов
        if found_by_channel:
            top_channels = sorted(found_by_channel.items(), key=lambda x: x[1], reverse=True)[:3]
            result_text += "<b>Топ каналов:</b>\n"
            for channel, count in top_channels:
                result_text += f"• @{channel}: {count} новостей\n"
        
        result_text += "\n<i>Новости отправляются в фоновом режиме...</i>"
        
    else:
        result_text = (
            f"📭 <b>Новых сообщений не найдено</b>\n\n"
            f"<b>Проверено каналов:</b> {len(channels)}\n"
            f"<b>Период:</b> последние 24 часа\n\n"
            f"<i>Советы:</i>\n"
            f"• Добавьте больше ключевых слов\n"
            f"• Расширьте список каналов\n"
            f"• Проверьте настройки исключений\n"
            f"• Попробуйте весовые ключевые слова"
        )
    
    await message.answer(result_text, parse_mode=ParseMode.HTML)
    
    # Предлагаем улучшения
    if total_found == 0 and len(keywords) < 5:
        await message.answer(
            "💡 <b>Совет:</b> Добавьте больше ключевых слов (минимум 5)\n"
            "Используйте кнопку «⚖️ Весовые теги» для точной настройки",
            parse_mode=ParseMode.HTML
        )

@router.message(F.text.startswith("@"))
async def handle_channel_input(message: Message, state: FSMContext):
    """Улучшенная обработка ввода канала"""
    channel = message.text.strip()
    user_id = message.from_user.id
    
    # Проверяем формат
    if not re.match(r'^@[a-zA-Z0-9_]{5,32}$', channel):
        await message.answer(
            "❌ <b>Некорректный формат</b>\n\n"
            "Username канала должен:\n"
            "• Начинаться с @\n"
            "• Содержать только буквы, цифры и _\n"
            "• Быть от 5 до 32 символов\n\n"
            "<b>Пример:</b> <code>@tproger</code>",
            parse_mode=ParseMode.HTML
        )
        return
    
    # Проверяем существование канала с детальной информацией
    await message.answer(f"🔍 Проверяю канал {channel}...")
    
    exists, info = await parser.check_channel_exists(channel)
    
    if not exists:
        await message.answer(
            f"❌ <b>Не удалось добавить канал</b>\n\n"
            f"<b>Причина:</b> {info}\n\n"
            f"<i>Убедитесь что канал публичный и username указан правильно</i>",
            parse_mode=ParseMode.HTML
        )
        return
    
    # Добавляем канал
    if db.add_channel(user_id, channel):
        response = (
            f"✅ <b>Канал {channel} успешно добавлен!</b>\n\n"
            f"{info}\n\n"
        )
        
        # Проверяем настройки пользователя
        keywords, _ = db.get_keywords(user_id)
        if not keywords:
            response += (
                f"💡 <b>Совет:</b> Настройте ключевые слова для поиска\n"
                f"Используйте «🏷️ Мои теги» → «✏️ Изменить теги»\n\n"
                f"<i>Без ключевых слов бот не будет находить релевантные новости</i>"
            )
        else:
            response += (
                f"Теперь можете проверить новости через «🔍 Проверить новости»\n"
                f"или настроить параметры поиска в «⚙️ Настройки»"
            )
        
        await message.answer(response, parse_mode=ParseMode.HTML)
    else:
        await message.answer(
            f"ℹ️ Канал {channel} уже был добавлен ранее\n\n"
            f"Используйте «📢 Мои каналы» для просмотра списка",
            parse_mode=ParseMode.HTML
        )
    
    await state.clear()

@router.message(F.text == "➕ Добавить канал")
async def cmd_add_channel(message: Message, state: FSMContext):
    """Добавление канала"""
    await message.answer(
        "➕ <b>Добавление канала</b>\n\n"
        "Отправьте username канала в формате:\n"
        "<code>@username</code>\n\n"
        "<b>Примеры:</b>\n"
        "<code>@tproger</code> - канал о программировании\n"
        "<code>@vcru</code> - Venture Capital\n"
        "<code>@roem_news</code> - IT новости\n\n"
        "<i>Канал должен быть публичным</i>",
        parse_mode=ParseMode.HTML
    )
    await state.set_state(UserStates.waiting_for_channel)

@router.message(F.text == "📢 Мои каналы")
async def cmd_my_channels(message: Message):
    """Мои каналы"""
    user_id = message.from_user.id
    channels = db.get_channels(user_id)
    
    if not channels:
        await message.answer(
            "📭 <b>У вас еще нет каналов</b>\n\n"
            "Добавьте каналы через кнопку «➕ Добавить канал»\n"
            "или отправьте @username канала",
            parse_mode=ParseMode.HTML
        )
        return
    
    channels_text = "📢 <b>Ваши каналы:</b>\n\n"
    for i, channel in enumerate(channels, 1):
        channels_text += f"{i}. @{channel}\n"
    
    channels_text += f"\n<b>Всего:</b> {len(channels)} каналов"
    
    await message.answer(
        channels_text, 
        parse_mode=ParseMode.HTML,
        reply_markup=get_channels_keyboard(channels)
    )

@router.message(F.text == "🏷️ Мои теги")
async def cmd_my_tags(message: Message):
    """Мои теги"""
    user_id = message.from_user.id
    keywords, negative = db.get_keywords(user_id)
    
    # Используем теги по умолчанию если нет своих
    if not keywords:
        keywords = config.DEFAULT_KEYWORDS
    
    keywords_text = ", ".join(keywords) if keywords else "не заданы"
    negative_text = ", ".join(negative) if negative else "не заданы"
    
    await message.answer(
        f"🏷️ <b>Ваши теги и фильтры</b>\n\n"
        f"<b>🔍 Ключевые слова для поиска:</b>\n"
        f"<code>{escape_html(keywords_text)}</code>\n\n"
        f"<b>🚫 Слова-исключения:</b>\n"
        f"<code>{escape_html(negative_text)}</code>\n\n"
        f"<i>Бот будет искать сообщения с ключевыми словами,\n"
        f"но без слов-исключений.</i>",
        parse_mode=ParseMode.HTML,
        reply_markup=get_settings_keyboard()
    )

@router.message(F.text == "📊 Статистика")
async def cmd_stats(message: Message):
    """Статистика"""
    user_id = message.from_user.id
    stats = db.get_user_stats(user_id)
    channels = db.get_channels(user_id)
    
    stats_text = f"📊 <b>Ваша статистика</b>\n\n"
    stats_text += f"<b>📢 Каналов:</b> {stats['channels']}\n"
    stats_text += f"<b>🏷️ Ключевых слов:</b> {stats['keywords']}\n"
    stats_text += f"<b>🚫 Исключений:</b> {stats['negative_keywords']}\n"
    stats_text += f"<b>📨 Отправлено новостей:</b> {stats['sent_news']}\n\n"
    
    if channels:
        stats_text += f"<b>Последние каналы:</b>\n"
        for i, channel in enumerate(channels[:5], 1):
            stats_text += f"{i}. @{channel}\n"
    
    await message.answer(stats_text, parse_mode=ParseMode.HTML)

@router.message(F.text == "⚙️ Настройки")
async def cmd_settings(message: Message):
    """Настройки"""
    await message.answer(
        "⚙️ <b>Настройки бота</b>\n\n"
        "Выберите действие:",
        parse_mode=ParseMode.HTML,
        reply_markup=get_settings_keyboard()
    )

@router.message(F.text == "❓ Помощь")
async def cmd_help_button(message: Message):
    """Кнопка помощи"""
    await cmd_help(message)

# ==================== CALLBACK HANDLERS ====================

@router.callback_query(F.data == "edit_keywords")
async def callback_edit_keywords(callback: CallbackQuery, state: FSMContext):
    """Редактирование ключевых слов"""
    await callback.message.answer(
        "✏️ <b>Введите ключевые слова:</b>\n\n"
        "<b>Формат:</b> слова через запятую\n"
        "<b>Пример:</b> технологии, программирование, стартап, инвестиции\n\n"
        "<i>Бот будет искать эти слова в сообщениях</i>",
        parse_mode=ParseMode.HTML
    )
    await state.set_state(UserStates.waiting_for_keywords)
    await callback.answer()

@router.callback_query(F.data == "edit_weighted_keywords")
async def callback_edit_weighted_keywords(callback: CallbackQuery, state: FSMContext):
    """Редактирование весовых ключевых слов"""
    await callback.message.answer(
        "⚖️ <b>Введите ключевые слова с весами:</b>\n\n"
        "<b>Формат:</b> слово:вес, слово:вес\n"
        "<b>Пример:</b> технологии:2.0, программирование:1.5, ИИ:3.0\n\n"
        "<i>Вес от 0.1 до 5.0 (по умолчанию 1.0)\n"
        "Чем выше вес, тем важнее ключевое слово</i>",
        parse_mode=ParseMode.HTML
    )
    await state.set_state(UserStates.waiting_for_weighted_keywords)
    await callback.answer()

@router.callback_query(F.data == "edit_negative")
async def callback_edit_negative(callback: CallbackQuery, state: FSMContext):
    """Редактирование исключений"""
    await callback.message.answer(
        "🚫 <b>Введите слова-исключения:</b>\n\n"
        "<b>Формат:</b> слова через запятую\n"
        "<b>Пример:</b> смерть, авария, преступление, война\n\n"
        "<i>Сообщения с этими словами будут игнорироваться</i>",
        parse_mode=ParseMode.HTML
    )
    await state.set_state(UserStates.waiting_for_negative)
    await callback.answer()

@router.callback_query(F.data.startswith("remove_channel:"))
async def callback_remove_channel(callback: CallbackQuery):
    """Удаление канала"""
    channel = callback.data.split(":")[1]
    user_id = callback.from_user.id
    
    if db.remove_channel(user_id, channel):
        await callback.message.edit_text(
            f"✅ Канал @{channel} удален\n\n"
            f"Обновите список каналов командой /channels",
            parse_mode=ParseMode.HTML
        )
    else:
        await callback.answer("❌ Канал не найден", show_alert=True)

@router.callback_query(F.data == "how_it_works")
async def callback_how_it_works(callback: CallbackQuery):
    """Как работает поиск"""
    await callback.answer()
    await callback.message.answer(
        "🤔 <b>Как работает поиск?</b>\n\n"
        "1. <b>Сбор сообщений</b> - бот получает последние сообщения из ваших каналов\n"
        "2. <b>Анализ текста</b> - ищет ваши ключевые слова в каждом сообщении\n"
        "3. <b>Фильтрация</b> - отбрасывает сообщения со словами-исключениями\n"
        "4. <b>Проверка повторов</b> - не показывает уже отправленные новости\n"
        "5. <b>Отправка</b> - отправляет подходящие сообщения вам\n\n"
        "<i>Поиск учитывает границы слов и регистр не важен</i>",
        parse_mode=ParseMode.HTML
    )

@router.callback_query(F.data == "add_more_channels")
async def callback_add_more_channels(callback: CallbackQuery):
    """Добавить еще каналов"""
    await callback.answer()
    await callback.message.answer(
        "Отправьте @username канала для добавления\n"
        "Пример: <code>@tproger</code>",
        parse_mode=ParseMode.HTML
    )

@router.callback_query(F.data == "parser_stats")
async def callback_parser_stats(callback: CallbackQuery):
    """Статистика парсера"""
    stats = parser.get_stats()
    
    stats_text = "📊 <b>Статистика парсера</b>\n\n"
    stats_text += f"<b>Запросы:</b>\n"
    stats_text += f"• ✅ Успешных: {stats.get('success', 0)}\n"
    stats_text += f"• ❌ Ошибок: {stats.get('failures', 0)}\n"
    stats_text += f"• ⏱️ Таймаутов: {stats.get('timeouts', 0)}\n\n"
    
    cache_stats = stats.get('cache_stats', {})
    stats_text += f"<b>Кэш:</b>\n"
    stats_text += f"• Размер: {cache_stats.get('size', 0)} записей\n"
    stats_text += f"• Хитов: {cache_stats.get('hits', 0)}\n"
    stats_text += f"• Промахов: {cache_stats.get('misses', 0)}\n"
    stats_text += f"• Хит-рейт: {cache_stats.get('hit_rate', 0):.1%}\n"
    
    await callback.message.answer(stats_text, parse_mode=ParseMode.HTML)
    await callback.answer()

@router.callback_query(F.data == "analytics_recommendations")
async def callback_analytics_recommendations(callback: CallbackQuery):
    """Рекомендации на основе аналитики"""
    user_id = callback.from_user.id
    stats = await analytics.get_detailed_stats(user_id)
    
    recommendations = []
    
    # Рекомендации на основе категорий
    top_categories = stats.get('top_categories', [])
    if top_categories:
        for category, count in top_categories[:2]:
            icon = config.CATEGORY_ICONS.get(category, '📝')
            
            # Рекомендуем каналы по категории
            if category == 'technology':
                recommendations.append(f"{icon} Попробуйте каналы: @tproger, @habr_com")
            elif category == 'business':
                recommendations.append(f"{icon} Попробуйте каналы: @vcru, @rbcdaily")
            elif category == 'news':
                recommendations.append(f"{icon} Попробуйте каналы: @rian_ru, @meduzalive")
    
    # Рекомендации по количеству каналов
    if stats['channels_count'] < 3:
        recommendations.append("💡 Добавьте больше каналов (минимум 3 для лучшего покрытия)")
    
    # Рекомендации по ключевым словам
    if stats['keywords_count'] < 5:
        recommendations.append("💡 Добавьте больше ключевых слов (рекомендуется 5-10)")
    
    # Формируем ответ
    if recommendations:
        response = "🎯 <b>Персональные рекомендации</b>\n\n"
        response += "\n".join(recommendations)
    else:
        response = "🤔 <b>Пока нет рекомендаций</b>\n\nДобавьте больше данных для персонализированных советов"
    
    await callback.message.answer(response, parse_mode=ParseMode.HTML)
    await callback.answer()

@router.callback_query(F.data.startswith("analytics_"))
async def handle_analytics_callbacks(callback: CallbackQuery):
    """Обработка callback-ов аналитики"""
    action = callback.data.split("_")[1]
    user_id = callback.from_user.id
    
    if action == "activity":
        await callback.answer("Функция в разработке", show_alert=True)
    elif action == "categories":
        stats = await analytics.get_detailed_stats(user_id)
        if stats['categories']:
            text = "🏷️ <b>Ваши интересы по категориям</b>\n\n"
            text += "\n".join(stats['categories'])
            await callback.message.answer(text, parse_mode=ParseMode.HTML)
        else:
            await callback.answer("Нет данных о категориях", show_alert=True)
    elif action == "top_channels":
        await callback.answer("Функция в разработке", show_alert=True)

# ==================== ОБРАБОТКА СОСТОЯНИЙ ====================

@router.message(UserStates.waiting_for_keywords)
async def process_keywords_input(message: Message, state: FSMContext):
    """Обработка ввода ключевых слов"""
    raw_keywords = [word.strip() for word in message.text.split(',')]
    keywords = [word for word in raw_keywords if word and len(word) >= 2]
    
    if not keywords:
        await message.answer(
            "❌ <b>Не найдено ключевых слов</b>\n\n"
            "Введите хотя бы одно слово длиной от 2 символов\n"
            "<b>Пример:</b> технологии, программирование",
            parse_mode=ParseMode.HTML
        )
        return
    
    if len(keywords) > 20:
        await message.answer(
            "❌ <b>Слишком много ключевых слов</b>\n\n"
            "Максимум 20 слов\n"
            "Выберите самые важные",
            parse_mode=ParseMode.HTML
        )
        return
    
    user_id = message.from_user.id
    db.set_keywords(user_id, keywords, is_negative=False)
    await state.clear()
    
    await message.answer(
        f"✅ <b>Ключевые слова обновлены!</b>\n\n"
        f"<b>Новый список ({len(keywords)} слов):</b>\n"
        f"<code>{escape_html(', '.join(keywords))}</code>\n\n"
        f"<i>Теперь проверьте новости через «🔍 Проверить новости»</i>",
        parse_mode=ParseMode.HTML
    )

@router.message(UserStates.waiting_for_weighted_keywords)
async def process_weighted_keywords_input(message: Message, state: FSMContext):
    """Обработка ввода весовых ключевых слов"""
    try:
        weighted_keywords = RelevanceAnalyzer.parse_weighted_keywords(message.text)
        
        if not weighted_keywords:
            await message.answer(
                "❌ <b>Не найдено ключевых слов</b>\n\n"
                "Введите хотя бы одно слово\n"
                "<b>Пример:</b> технологии:2.0, программирование:1.5",
                parse_mode=ParseMode.HTML
            )
            return
        
        if len(weighted_keywords) > 20:
            await message.answer(
                "❌ <b>Слишком много ключевых слов</b>\n\n"
                "Максимум 20 слов\n"
                "Выберите самые важные",
                parse_mode=ParseMode.HTML
            )
            return
        
        user_id = message.from_user.id
        keywords = [kw[0] for kw in weighted_keywords]
        
        # Сохраняем плоский список (для обратной совместимости)
        db.set_keywords(user_id, keywords, is_negative=False)
        
        # Форматируем ответ с весами
        keywords_text = "\n".join([
            f"• {escape_html(kw)}: <code>{weight:.1f}</code>" for kw, weight in weighted_keywords
        ])
        
        await state.clear()
        
        await message.answer(
            f"✅ <b>Весовые ключевые слова обновлены!</b>\n\n"
            f"<b>Новый список ({len(weighted_keywords)} слов):</b>\n"
            f"{keywords_text}\n\n"
            f"<i>Теперь поиск будет учитывать важность каждого слова</i>",
            parse_mode=ParseMode.HTML
        )
        
    except Exception as e:
        await message.answer(
            f"❌ <b>Ошибка обработки</b>\n\n"
            f"Проверьте формат ввода\n"
            f"<b>Пример:</b> <code>технологии:2.0, программирование:1.5</code>",
            parse_mode=ParseMode.HTML
        )

@router.message(UserStates.waiting_for_negative)
async def process_negative_input(message: Message, state: FSMContext):
    """Обработка ввода исключений"""
    raw_keywords = [word.strip() for word in message.text.split(',')]
    negative = [word for word in raw_keywords if word and len(word) >= 2]
    
    if not negative:
        await message.answer(
            "❌ <b>Не найдено слов-исключений</b>\n\n"
            "Введите хотя бы одно слово\n"
            "<b>Пример:</b> смерть, авария, война",
            parse_mode=ParseMode.HTML
        )
        return
    
    user_id = message.from_user.id
    db.set_keywords(user_id, negative, is_negative=True)
    await state.clear()
    
    await message.answer(
        f"✅ <b>Слова-исключения обновлены!</b>\n\n"
        f"<b>Новый список ({len(negative)} слов):</b>\n"
        f"<code>{escape_html(', '.join(negative))}</code>\n\n"
        f"<i>Сообщения с этими словами теперь будут игнорироваться</i>",
        parse_mode=ParseMode.HTML
    )

# ==================== ОБРАБОТКА ПРОЧИХ СООБЩЕНИЙ ====================

@router.message()
async def handle_other_messages(message: Message):
    """Обработка прочих сообщений"""
    # Если сообщение похоже на username канала
    text = message.text.strip()
    if re.match(r'^@[a-zA-Z0-9_]{5,}$', text):
        # Используем FSMContext из сообщения
        from aiogram.fsm.context import FSMContext
        from aiogram.fsm.storage.memory import MemoryStorage
        
        storage = MemoryStorage()
        fsm_context = FSMContext(storage=storage, key=f"fsm:{message.from_user.id}")
        await handle_channel_input(message, fsm_context)
        return
    
    # Помощь по непонятным командам
    if len(text) < 50:  # Только для коротких сообщений
        await message.answer(
            "🤖 <b>Я не понял команду</b>\n\n"
            "Используйте кнопки меню или команды:\n"
            "/start - Начало работы\n"
            "/help - Помощь\n"
            "/channels - Мои каналы\n"
            "/tags - Мои теги\n\n"
            "Или отправьте @username канала для добавления",
            parse_mode=ParseMode.HTML,
            reply_markup=get_main_keyboard()
        )

# ==================== ЗАПУСК ====================

async def main():
    logger.info("🚀 Улучшенный бот запущен и готов к работе!")
    
    # Инициализация базы данных
    try:
        cleaned = db.cleanup_old_news(days=30)
        logger.info(f"✅ База данных инициализирована. Очищено {cleaned} старых записей")
    except Exception as e:
        logger.error(f"Ошибка инициализации БД: {e}")
    
    # Создаем бота и диспетчер
    bot = Bot(token=config.BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    
    # Инициализируем менеджер очереди
    global news_queue
    news_queue = NewsQueueManager(bot)
    news_queue.set_bot(bot)
    
    # Запускаем обработчик очереди в фоне
    queue_task = None
    try:
        queue_task = asyncio.create_task(
            news_queue.process_queue(
                batch_size=config.SEND_BATCH_SIZE,
                delay=config.SEND_DELAY
            )
        )
        logger.info("🚀 Обработчик очереди запущен")
    except Exception as e:
        logger.error(f"Ошибка запуска обработчика очереди: {e}")
    
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    except Exception as e:
        logger.error(f"Ошибка при запуске polling: {e}")
        raise
    finally:
        # Останавливаем обработчик очереди
        if news_queue:
            news_queue.stop_processing()
            if queue_task:
                try:
                    await queue_task
                except Exception as e:
                    logger.error(f"Ошибка при остановке очереди: {e}")
        
        try:
            await parser.close_session()
        except Exception as e:
            logger.error(f"Ошибка при закрытии сессии парсера: {e}")
        
        try:
            await bot.session.close()
        except Exception as e:
            logger.error(f"Ошибка при закрытии сессии бота: {e}")
        
        logger.info("👋 Бот остановлен")

if __name__ == "__main__":
    asyncio.run(main())