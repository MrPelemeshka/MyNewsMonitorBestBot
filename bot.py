import asyncio
import logging
import re
import html
from datetime import datetime, timedelta
from typing import List, Dict
from collections import defaultdict

from aiogram import Bot, Dispatcher, types, F, Router
from aiogram.filters import Command
from aiogram.types import (
    Message, InlineKeyboardMarkup, InlineKeyboardButton, 
    ReplyKeyboardMarkup, KeyboardButton, CallbackQuery
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

# Вспомогательные функции
def escape_html(text: str) -> str:
    """Экранирование HTML символов"""
    return html.escape(text)

# Класс для форматирования новостей
class NewsFormatter:
    """Форматировщик новостей с информацией о файлах"""
    
    @staticmethod
    def format_news_card(msg: Dict, found_keywords: List[str] = None) -> str:
        """Форматирование новости в виде карточки с указанием ключевых слов и файлов"""
        # Если нет текста, используем заголовок по умолчанию
        if msg['text']:
            title = NewsFormatter._extract_title(msg['text'])
        else:
            title = "Сообщение с файлом"
        
        channel = msg.get('channel', 'unknown')
        
        # Форматируем время
        time_str = ""
        msg_time = msg.get('timestamp_naive', msg.get('timestamp'))
        if msg_time:
            now = datetime.now()
            
            if msg_time.tzinfo is not None:
                from datetime import timezone
                msg_time = msg_time.astimezone(timezone.utc).replace(tzinfo=None)
            
            if now.date() == msg_time.date():
                time_str = f"Сегодня в {msg_time.strftime('%H:%M')}"
            elif (now - timedelta(days=1)).date() == msg_time.date():
                time_str = f"Вчера в {msg_time.strftime('%H:%M')}"
            else:
                time_str = msg_time.strftime("%d.%m.%Y в %H:%M")
        
        # Собираем HTML сообщение
        parts = []
        
        # Заголовок
        parts.append(f"📰 <b>{escape_html(title)}</b>\n")
        
        # Источник и время
        parts.append(f"📢 @{channel}  ⏰ {time_str}\n")
        
        # Показываем информацию о файлах
        if msg.get('has_file'):
            file_icons = {
                'photo': '📷',
                'video': '🎬',
                'document': '📄',
                'audio': '🎵',
                'voice': '🎤',
                'sticker': '🖼️'
            }
            
            file_types = msg.get('file_types', [])
            if file_types:
                file_info = []
                for file_type in file_types:
                    icon = file_icons.get(file_type, '📎')
                    # Русские названия для понятности
                    type_names = {
                        'photo': 'фото',
                        'video': 'видео',
                        'document': 'документ',
                        'audio': 'аудио',
                        'voice': 'голосовое',
                        'sticker': 'стикер'
                    }
                    name = type_names.get(file_type, file_type)
                    file_info.append(f"{icon} {name}")
                
                parts.append(f"📎 <b>Файлы:</b> {', '.join(file_info)}\n")
            else:
                parts.append(f"📎 <b>С файлом</b>\n")
        
        # Показываем найденные ключевые слова (если есть)
        if found_keywords:
            # Фильтруем и форматируем ключевые слова для отображения
            display_keywords = []
            for kw in found_keywords:
                if kw == "$файл":
                    display_keywords.append("📎 файл")
                else:
                    display_keywords.append(kw)
            
            keywords_text = ", ".join([f"<code>{escape_html(kw)}</code>" for kw in display_keywords[:5]])
            
            # Указываем причину попадания в подборку
            if "$файл" in found_keywords and len(found_keywords) == 1:
                parts.append(f"✅ <b>Найдено:</b> сообщение с файлом\n\n")
            else:
                parts.append(f"✅ <b>Найдено по словам:</b> {keywords_text}\n\n")
        else:
            parts.append("\n")
        
        # Основной текст (если есть)
        if msg['text']:
            excerpt = msg['text'][:300].strip()
            if len(msg['text']) > 300:
                excerpt += "..."
            parts.append(f"{escape_html(excerpt)}\n")
        elif msg.get('has_file'):
            parts.append("<i>Сообщение содержит только файл(ы)</i>\n")
        
        # Ссылка
        if msg.get('url'):
            parts.append(f"\n🔗 <a href='{escape_html(msg['url'])}'>Смотреть в канале</a>")
        
        return "".join(parts)

# Класс для анализа релевантности
class RelevanceAnalyzer:
    """Анализатор релевантности с поддержкой специального тега $файл"""
    
    @staticmethod
    def analyze_message(text: str, keywords: List[str], 
                       negative_keywords: List[str], 
                       has_file: bool = False) -> Dict:
        """Анализ сообщения с поддержкой $файл (логика ИЛИ)"""
        text_lower = f" {text.lower()} "
        
        # Проверяем наличие специального тега $файл
        has_file_keyword = "$файл" in keywords or "$file" in keywords
        
        # Поиск обычных ключевых слов (исключая специальные теги)
        found_keywords = []
        
        for keyword in keywords:
            keyword_lower = keyword.lower()
            
            # Пропускаем специальные теги
            if keyword in ["$файл", "$file"]:
                continue
            
            # Ищем слово как отдельное
            if f" {keyword_lower} " in text_lower:
                found_keywords.append(keyword)
            # Или как часть слова
            elif keyword_lower in text_lower:
                found_keywords.append(keyword)
        
        # Проверяем, релевантно ли сообщение по тексту
        relevant_by_text = len(found_keywords) > 0
        
        # Проверяем, релевантно ли сообщение по файлу
        relevant_by_file = has_file_keyword and has_file
        
        # Сообщение релевантно если:
        # 1. Есть обычные ключевые слова ИЛИ
        # 2. Есть $файл в ключевых словах И есть файл в сообщении
        # (Логика ИЛИ, а не И)
        is_relevant = relevant_by_text or relevant_by_file
        
        # Если сообщение релевантно по файлу, добавляем $файл в найденные ключевые слова
        if relevant_by_file:
            found_keywords.append("$файл")
        
        # Проверка отрицательных ключевых слов
        found_negative = []
        for neg_keyword in negative_keywords:
            neg_lower = neg_keyword.lower()
            if f" {neg_lower} " in text_lower or neg_lower in text_lower:
                found_negative.append(neg_keyword)
        
        return {
            'relevant': is_relevant and len(found_negative) == 0,
            'found_keywords': found_keywords,
            'found_negative': found_negative,
            'keyword_count': len(found_keywords),
            'has_negative': len(found_negative) > 0,
            'has_file': has_file,
            'relevant_by_text': relevant_by_text,
            'relevant_by_file': relevant_by_file
        }

# Класс для управления очередью отправки
class NewsQueueManager:
    """Менеджер очереди отправки новостей"""
    
    def __init__(self, bot: Bot = None):
        self.queue = asyncio.Queue()
        self.processing = False
        self.bot = bot
    
    def set_bot(self, bot: Bot):
        """Установить бота"""
        self.bot = bot
    
    async def add_news_batch(self, user_id: int, news_items: List[Dict]):
        """Добавление партии новостей в очередь"""
        if not news_items or not self.bot:
            return
        
        for item in news_items:
            try:
                news_item = item['news_item']
                
                # Форматируем сообщение
                message_text = NewsFormatter.format_news_card(news_item['message'])
                
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
                
                # Пауза между сообщениями
                await asyncio.sleep(0.2)
                
            except Exception as e:
                logger.error(f"Ошибка отправки новости: {e}")
                continue
    
    def stop_processing(self):
        """Остановка обработки очереди"""
        self.processing = False

# Клавиатуры
def get_main_keyboard() -> ReplyKeyboardMarkup:
    """Основная клавиатура"""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🔍 Проверить новости"), KeyboardButton(text="📊 Статистика")],
            [KeyboardButton(text="📢 Мои каналы"), KeyboardButton(text="🏷️ Мои теги")],
            [KeyboardButton(text="➕ Добавить канал"), KeyboardButton(text="❓ Помощь")]
        ],
        resize_keyboard=True,
        input_field_placeholder="Выберите действие..."
    )

def get_channels_keyboard(channels: List[str], page: int = 1, total_pages: int = 1) -> InlineKeyboardMarkup:
    """Клавиатура для управления каналами с пагинацией"""
    builder = InlineKeyboardBuilder()
    
    # Добавляем кнопки для каналов на текущей странице
    for channel in channels:
        builder.button(
            text=f"❌ @{channel}",
            callback_data=f"remove_channel:{channel}"
        )
    
    builder.adjust(2)  # 2 кнопки в ряд
    
    # Кнопки пагинации
    pagination_buttons = []
    
    if page > 1:
        pagination_buttons.append(
            InlineKeyboardButton(text="⬅️ Назад", callback_data=f"channels_page:{page-1}")
        )
    
    pagination_buttons.append(
        InlineKeyboardButton(text=f"{page}/{total_pages}", callback_data="current_page")
    )
    
    if page < total_pages:
        pagination_buttons.append(
            InlineKeyboardButton(text="Вперёд ➡️", callback_data=f"channels_page:{page+1}")
        )
    
    if pagination_buttons:
        builder.row(*pagination_buttons)
    
    return builder.as_markup()

def get_settings_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура настроек"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✏️ Изменить теги", callback_data="edit_keywords"),
            InlineKeyboardButton(text="🚫 Исключения", callback_data="edit_negative")
        ],
        [
            InlineKeyboardButton(text="❓ Как работает", callback_data="how_it_works")
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
        reply_markup=get_main_keyboard()
    )
    
    # Предлагаем начать с добавления канала
    channels = db.get_all_channels(user_id)
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

@router.message(Command("channels"))
async def cmd_channels(message: Message):
    """Команда /channels - список каналов с пагинацией"""
    user_id = message.from_user.id
    page = 1
    channels, total_channels, total_pages = db.get_channels(user_id, page=page)
    
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
    
    channels_text = f"📢 <b>Ваши каналы (страница {page}/{total_pages}):</b>\n\n"
    for i, channel in enumerate(channels, 1):
        channels_text += f"{i}. @{channel}\n"
    
    channels_text += f"\n<b>Всего каналов:</b> {total_channels}"
    
    await message.answer(
        channels_text, 
        parse_mode=ParseMode.HTML,
        reply_markup=get_channels_keyboard(channels, page, total_pages)
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
    
    stats_text = f"📊 <b>Ваша статистика</b>\n\n"
    stats_text += f"<b>📢 Каналов:</b> {stats['channels']}\n"
    stats_text += f"<b>🏷️ Ключевых слов:</b> {stats['keywords']}\n"
    stats_text += f"<b>🚫 Исключений:</b> {stats['negative_keywords']}\n"
    stats_text += f"<b>📨 Отправлено новостей:</b> {stats['sent_news']}\n"
    
    await message.answer(stats_text, parse_mode=ParseMode.HTML)

# ==================== КНОПКИ ====================

@router.message(F.text == "🔍 Проверить новости")
async def cmd_check_news(message: Message):
    """Проверка новостей"""
    user_id = message.from_user.id
    channels = db.get_all_channels(user_id)
    
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
    
    # Анализируем настройки поиска
    has_file_search = "$файл" in keywords or "$file" in keywords
    text_keywords = [kw for kw in keywords if kw not in ["$файл", "$file"]]
    
    # Статус начала проверки
    status_msg = await message.answer(
        f"🔍 <b>Начинаю проверку...</b>\n\n"
        f"<b>Каналов:</b> {len(channels)}\n"
        f"<b>Ключевых слов:</b> {len(text_keywords)}\n"
        f"<b>Поиск файлов:</b> {'да' if has_file_search else 'нет'}\n"
        f"<b>Исключений:</b> {len(negative)}",
        parse_mode=ParseMode.HTML
    )
    
    total_found = 0
    found_by_text = 0
    found_by_file = 0
    
    # Проверяем каждый канал
    for i, channel in enumerate(channels, 1):
        try:
            # Получаем свежие сообщения
            messages = await parser.get_fresh_messages(channel, hours=24, limit=20)
            
            for msg in messages:
                # Анализируем с учетом файлов
                analysis = RelevanceAnalyzer.analyze_message(
                    msg['text'],
                    keywords,
                    negative,
                    has_file=msg.get('has_file', False)
                )
                
                if analysis['relevant'] and not analysis['has_negative']:
                    msg_time = msg.get('timestamp_naive', msg.get('timestamp'))
                    if msg_time:
                        age = datetime.now() - msg_time
                        if age.days > 7:
                            continue
                    
                    news_hash = db.generate_news_hash(msg['text'], channel, msg.get('id'))
                    
                    if not db.is_news_sent(user_id, news_hash):
                        # Получаем найденные ключевые слова
                        found_keywords = analysis.get('found_keywords', [])
                        
                        # Статистика
                        if analysis['relevant_by_file'] and not analysis['relevant_by_text']:
                            found_by_file += 1
                        else:
                            found_by_text += 1
                        
                        # Отправляем новость с указанием ключевых слов
                        message_text = NewsFormatter.format_news_card(msg, found_keywords)
                        
                        try:
                            await message.answer(
                                message_text,
                                parse_mode=ParseMode.HTML,
                                disable_web_page_preview=False
                            )
                            
                            db.mark_news_sent(user_id, news_hash, channel, msg.get('id'))
                            total_found += 1
                            
                            await asyncio.sleep(0.2)
                            
                        except Exception as e:
                            logger.error(f"Ошибка отправки: {e}")
                            continue
            
            # Обновляем статус
            if i % 3 == 0 or i == len(channels):
                try:
                    await status_msg.edit_text(
                        f"🔍 <b>Проверяю...</b>\n\n"
                        f"<b>Прогресс:</b> {i}/{len(channels)}\n"
                        f"<b>Найдено новостей:</b> {total_found}\n"
                        f"<b>Из них с файлами:</b> {found_by_file}",
                        parse_mode=ParseMode.HTML
                    )
                except:
                    pass
            
            # Пауза между каналами
            await asyncio.sleep(0.5)
            
        except Exception as e:
            logger.error(f"Ошибка проверки канала @{channel}: {e}")
            continue
    
    # Итоговый результат с детальной статистикой
    if total_found > 0:
        result_text = (
            f"✅ <b>Проверка завершена!</b>\n\n"
            f"<b>Найдено новых сообщений:</b> {total_found}\n"
        )
        
        if has_file_search:
            result_text += (
                f"• По ключевым словам: {found_by_text}\n"
                f"• С файлами: {found_by_file}\n"
            )
        
        result_text += f"<b>Всего проверено:</b> {len(channels)}\n\n"
        
        # Добавляем информацию о логике поиска
        if has_file_search:
            result_text += (
                f"<i>📌 Логика поиска: ИЛИ</i>\n"
                f"<i>Сообщения показываются если содержат ключевые слова ИЛИ имеют файлы</i>\n"
                f"<i>💡 Каждое сообщение помечено, по какой причине оно было найдено</i>"
            )
        else:
            result_text += (
                f"<i>📌 В каждой новости указаны ключевые слова, по которым она была найдена</i>"
            )
    else:
        result_text = (
            f"📭 <b>Новых сообщений не найдено</b>\n\n"
            f"<b>Проверено каналов:</b> {len(channels)}\n"
            f"<b>Период:</b> последние 24 часа\n\n"
            f"<i>Советы:</i>\n"
            f"• Добавьте больше ключевых слов\n"
            f"• Расширьте список каналов\n"
            f"• Проверьте настройки исключений\n"
        )
        
        if not has_file_search:
            result_text += f"• Добавьте тег <code>$файл</code> для поиска сообщений с файлами"
    
    await message.answer(result_text, parse_mode=ParseMode.HTML)

@router.message(F.text.startswith("@"))
async def handle_channel_input(message: Message, state: FSMContext):
    """Обработка ввода канала"""
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
    
    # Проверяем существование канала
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
                f"Теперь можете проверить новости через «🔍 Проверить новости»"
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
        "<i>💡 Совет: Добавьте тег <code>$файл</code> в ключевые слова,\n"
        "чтобы получать сообщения с файлами (фото, видео, документы)</i>",
        parse_mode=ParseMode.HTML
    )
    await state.set_state(UserStates.waiting_for_channel)

@router.message(F.text == "📢 Мои каналы")
async def cmd_my_channels(message: Message):
    """Мои каналы с пагинацией"""
    await cmd_channels(message)

@router.message(F.text == "🏷️ Мои теги")
async def cmd_my_tags(message: Message):
    """Мои теги"""
    await cmd_tags(message)

@router.message(F.text == "📊 Статистика")
async def cmd_stats(message: Message):
    """Статистика"""
    await cmd_stats_command(message)

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

@router.callback_query(F.data.startswith("channels_page:"))
async def callback_channels_page(callback: CallbackQuery):
    """Пагинация каналов"""
    user_id = callback.from_user.id
    page = int(callback.data.split(":")[1])
    
    channels, total_channels, total_pages = db.get_channels(user_id, page=page)
    
    channels_text = f"📢 <b>Ваши каналы (страница {page}/{total_pages}):</b>\n\n"
    for i, channel in enumerate(channels, 1):
        channels_text += f"{i}. @{channel}\n"
    
    channels_text += f"\n<b>Всего каналов:</b> {total_channels}"
    
    try:
        await callback.message.edit_text(
            channels_text,
            parse_mode=ParseMode.HTML,
            reply_markup=get_channels_keyboard(channels, page, total_pages)
        )
    except:
        pass
    
    await callback.answer()

@router.callback_query(F.data.startswith("remove_channel:"))
async def callback_remove_channel(callback: CallbackQuery):
    """Удаление канала"""
    channel = callback.data.split(":")[1]
    user_id = callback.from_user.id
    
    # Получаем текущую страницу для возврата
    current_page = 1
    
    if db.remove_channel(user_id, channel):
        # Получаем обновленный список
        channels, total_channels, total_pages = db.get_channels(user_id, page=current_page)
        
        if channels:
            channels_text = f"📢 <b>Ваши каналы (страница {current_page}/{total_pages}):</b>\n\n"
            for i, channel_name in enumerate(channels, 1):
                channels_text += f"{i}. @{channel_name}\n"
            
            channels_text += f"\n<b>Всего каналов:</b> {total_channels}"
            
            await callback.message.edit_text(
                f"✅ Канал @{channel} удален\n\n{channels_text}",
                parse_mode=ParseMode.HTML,
                reply_markup=get_channels_keyboard(channels, current_page, total_pages)
            )
        else:
            await callback.message.edit_text(
                f"✅ Канал @{channel} удален\n\n📭 <b>У вас больше нет каналов</b>\n"
                f"Добавьте новый канал через «➕ Добавить канал»",
                parse_mode=ParseMode.HTML
            )
    else:
        await callback.answer("❌ Канал не найден", show_alert=True)
    
    await callback.answer()

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
            "<b>Пример:</b> технологии, программирование\n\n"
            "<i>💡 Добавьте <code>$файл</code> для поиска сообщений с файлами</i>",
            parse_mode=ParseMode.HTML
        )
        return
    
    user_id = message.from_user.id
    db.set_keywords(user_id, keywords, is_negative=False)
    await state.clear()
    
    # Проверяем, есть ли $файл в ключевых словах
    has_file_tag = "$файл" in keywords or "$file" in keywords
    
    response = (
        f"✅ <b>Ключевые слова обновлены!</b>\n\n"
        f"<b>Новый список ({len(keywords)} слов):</b>\n"
        f"<code>{escape_html(', '.join(keywords))}</code>\n\n"
    )
    
    if has_file_tag:
        response += (
            f"<i>📌 Логика поиска: ИЛИ</i>\n"
            f"<i>Бот будет показывать сообщения которые содержат ключевые слова ИЛИ имеют файлы</i>\n"
            f"<i>Теперь проверьте новости через «🔍 Проверить новости»</i>"
        )
    else:
        response += (
            f"<i>Теперь проверьте новости через «🔍 Проверить новости»</i>"
        )
    
    await message.answer(response, parse_mode=ParseMode.HTML)

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

# ==================== ЗАПУСК ====================

async def main():
    logger.info("🚀 Бот запущен и готов к работе!")
    
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
    
    # Создаем менеджер очереди
    news_queue = NewsQueueManager(bot)
    
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    except Exception as e:
        logger.error(f"Ошибка при запуске polling: {e}")
        raise
    finally:
        # Останавливаем обработчик очереди
        news_queue.stop_processing()
        
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