import os
import logging
import sqlite3
from pathlib import Path
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, types
from aiogram.contrib.middlewares.logging import LoggingMiddleware
from aiogram.utils import executor
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

# Загружаем переменные окружения
load_dotenv()

# --- Переменные окружения ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не задан")
if ADMIN_ID == 0:
    logging.warning("ADMIN_ID не задан")

# --- Настройка хранения данных (Bothost) ---
DATA_DIR = Path(os.getenv("DATA_DIR", "/app/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "game.db"

# --- Инициализация бота ---
logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot)
dp.middleware.setup(LoggingMiddleware())

# ==================== БАЗА ДАННЫХ ====================

def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    """Создаёт таблицы, если они ещё не существуют (без данных)"""
    conn = get_db_connection()
    c = conn.cursor()
    
    c.execute('''
        CREATE TABLE IF NOT EXISTS locations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS resources (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS mobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            location_id INTEGER NOT NULL,
            hp INTEGER,
            dust INTEGER,
            exp INTEGER,
            resource_id INTEGER,
            FOREIGN KEY(location_id) REFERENCES locations(id),
            FOREIGN KEY(resource_id) REFERENCES resources(id)
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS drops (
            mob_id INTEGER,
            resource_id INTEGER,
            chance REAL DEFAULT 100,
            PRIMARY KEY (mob_id, resource_id),
            FOREIGN KEY(mob_id) REFERENCES mobs(id),
            FOREIGN KEY(resource_id) REFERENCES resources(id)
        )
    ''')
    conn.commit()
    conn.close()
    logging.info("Схема БД проверена/создана")

# ==================== КОМАНДЫ БОТА ====================

@dp.message_handler(commands=['start'])
async def cmd_start(message: types.Message):
    await message.reply(
        "👋 Привет! Я бот с базой знаний по игре.\n\n"
        "🔍 Доступные команды:\n"
        "/resources – список ресурсов и кто их даёт\n"
        "/mobs – список всех мобов\n"
        "/mob <название> – найти моба по имени\n\n"
        "Пример: /mob светлячок",
        parse_mode=None  # Отключаем Markdown
    )

@dp.message_handler(commands=['resources'])
async def cmd_resources(message: types.Message):
    """Показывает все ресурсы, а под каждым – моба и локацию"""
    conn = get_db_connection()
    c = conn.cursor()
    query = '''
        SELECT r.name AS resource_name,
               m.name AS mob_name,
               l.name AS location_name
        FROM resources r
        JOIN drops d ON d.resource_id = r.id
        JOIN mobs m ON m.id = d.mob_id
        JOIN locations l ON l.id = m.location_id
        ORDER BY r.name
    '''
    c.execute(query)
    rows = c.fetchall()
    conn.close()
    
    if not rows:
        await message.answer("В базе пока нет ресурсов. Данные добавляются отдельно.")
        return
    
    response = "📦 *Список ресурсов:*\n\n"
    for row in rows:
        response += f"🌿 *{row['resource_name']}*\n"
        response += f"   🧟 Моб: {row['mob_name']}\n"
        response += f"   📍 Локация: {row['location_name']}\n\n"
    
    if len(response) > 4096:
        # Если слишком длинное – разбиваем (можно добавить пагинацию)
        await message.answer(response[:4000] + "\n... (обрезано)")
    else:
        await message.answer(response, parse_mode="Markdown")

@dp.message_handler(commands=['mobs'])
async def cmd_mobs_list(message: types.Message):
    """Выводит список мобов в виде инлайн-кнопок. При нажатии – карточка"""
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT id, name FROM mobs ORDER BY name")
    mobs = c.fetchall()
    conn.close()
    
    if not mobs:
        await message.answer("Список мобов пуст. Данные добавляются отдельно.")
        return
    
    keyboard = InlineKeyboardMarkup(row_width=2)
    for mob in mobs:
        keyboard.add(InlineKeyboardButton(mob['name'], callback_data=f"mob_{mob['id']}"))
    
    await message.answer("📋 *Выберите моба:*", reply_markup=keyboard, parse_mode="Markdown")

@dp.callback_query_handler(lambda c: c.data and c.data.startswith('mob_'))
async def show_mob_card(callback_query: types.CallbackQuery):
    mob_id = int(callback_query.data.split('_')[1])
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''
        SELECT m.name, m.hp, m.dust, m.exp,
               l.name AS location_name,
               r.name AS resource_name
        FROM mobs m
        JOIN locations l ON l.id = m.location_id
        LEFT JOIN resources r ON r.id = m.resource_id
        WHERE m.id = ?
    ''', (mob_id,))
    mob = c.fetchone()
    conn.close()
    
    if not mob:
        await callback_query.answer("Моб не найден", show_alert=True)
        return
    
    text = f"👾 *{mob['name']}*\n"
    text += f"📍 *Локация:* {mob['location_name']}\n"
    text += f"❤️ *HP:* {mob['hp']}\n"
    text += f"💰 *Пыль (золото):* {mob['dust']}\n"
    text += f"✨ *Опыт:* {mob['exp']}\n"
    text += f"🎁 *Дроп:* {mob['resource_name'] if mob['resource_name'] else 'Нет данных'}\n"
    
    await callback_query.message.answer(text, parse_mode="Markdown")
    await callback_query.answer()

@dp.message_handler(commands=['mob'])
async def cmd_mob_search(message: types.Message):
    query = message.get_args().strip()
    if not query:
        await message.answer("Укажите имя моба, например: `/mob светлячок`", parse_mode="Markdown")
        return
    
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''
        SELECT m.id, m.name, m.hp, m.dust, m.exp,
               l.name AS location_name,
               r.name AS resource_name
        FROM mobs m
        JOIN locations l ON l.id = m.location_id
        LEFT JOIN resources r ON r.id = m.resource_id
        WHERE LOWER(m.name) LIKE LOWER(?)
    ''', (f'%{query}%',))
    mob = c.fetchone()
    conn.close()
    
    if not mob:
        await message.answer(f"Моб `{query}` не найден.", parse_mode="Markdown")
        return
    
    text = f"👾 *{mob['name']}*\n"
    text += f"📍 *Локация:* {mob['location_name']}\n"
    text += f"❤️ *HP:* {mob['hp']}\n"
    text += f"💰 *Пыль (золото):* {mob['dust']}\n"
    text += f"✨ *Опыт:* {mob['exp']}\n"
    text += f"🎁 *Дроп:* {mob['resource_name'] if mob['resource_name'] else 'Нет данных'}\n"
    await message.answer(text, parse_mode="Markdown")

@dp.message_handler(commands=['help'])
async def cmd_help(message: types.Message):
    await cmd_start(message)

# ==================== ЗАПУСК ====================

async def on_startup(dp):
    init_db()
    logging.info("Бот запущен. База данных готова (без данных).")
    # Проверка, есть ли данные – можно отправить админу уведомление, если пусто
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM mobs")
    count = c.fetchone()[0]
    conn.close()
    if count == 0:
        logging.warning("В базе нет данных. Заполните её через SQL-скрипты или админ-команды.")

if __name__ == '__main__':
    executor.start_polling(dp, on_startup=on_startup, skip_updates=True)
