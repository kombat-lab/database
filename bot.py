import os
import logging
import sqlite3
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, types
from aiogram.contrib.middlewares.logging import LoggingMiddleware
from aiogram.utils import executor

# Загружаем переменные окружения
load_dotenv()

# --- Переменные окружения ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

# Проверка обязательных переменных
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не задан в переменных окружения")
if ADMIN_ID == 0:
    raise ValueError("ADMIN_ID не задан в переменных окружения")

# --- Инициализация бота ---
logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot)
dp.middleware.setup(LoggingMiddleware())

# --- Инициализация базы данных SQLite ---
DB_PATH = "game.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    # Таблица мобов
    c.execute('''CREATE TABLE IF NOT EXISTS mobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE,
        location TEXT,
        level INTEGER,
        description TEXT
    )''')
    # Таблица дропа
    c.execute('''CREATE TABLE IF NOT EXISTS drops (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        mob_id INTEGER,
        item_name TEXT,
        chance TEXT,
        FOREIGN KEY(mob_id) REFERENCES mobs(id)
    )''')
    conn.commit()
    conn.close()

def load_initial_data():
    """Загружает тестовых мобов, если база пуста"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM mobs")
    if c.fetchone()[0] == 0:
        test_mobs = [
            ("Лесной голем", "Тёмный лес", 15, "Огромное каменное существо, охраняющее древние руины.",
             [("древесный уголь", "100%"), ("камень души", "5%")]),
            ("Огненный слизень", "Пепельные земли", 22, "Раскалённая субстанция, оставляющая за собой след из лавы.",
             [("огненная слизь", "80%"), ("искра магии", "30%")])
        ]
        for name, loc, lvl, desc, drops_list in test_mobs:
            c.execute("INSERT INTO mobs (name, location, level, description) VALUES (?,?,?,?)",
                      (name, loc, lvl, desc))
            mob_id = c.lastrowid
            for item, chance in drops_list:
                c.execute("INSERT INTO drops (mob_id, item_name, chance) VALUES (?,?,?)",
                          (mob_id, item, chance))
        conn.commit()
        logging.info("Тестовые данные загружены в SQLite")
    conn.close()

# --- Команда /start ---
@dp.message_handler(commands=['start'])
async def cmd_start(message: types.Message):
    await message.reply(
        "👋 Привет! Я бот с базой знаний по игре.\n\n"
        "🔍 Команды:\n"
        "/mob <название> - найти информацию о мобе\n"
        "/list_mobs - показать всех мобов\n"
        "/help - справка\n\n"
        "Пример: /mob голем"
    )

# --- Команда /help ---
@dp.message_handler(commands=['help'])
async def cmd_help(message: types.Message):
    await message.reply(
        "📖 *Справка*\n\n"
        "/mob <название> - поиск моба по части названия\n"
        "/list_mobs - список всех мобов\n\n"
        "Скоро появятся разделы: предметы, крафт, локации.",
        parse_mode="Markdown"
    )

# --- Команда /list_mobs ---
@dp.message_handler(commands=['list_mobs'])
async def cmd_list_mobs(message: types.Message):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT name FROM mobs ORDER BY name")
    rows = c.fetchall()
    conn.close()
    if not rows:
        await message.answer("Список мобов пуст.")
        return
    response = "📋 *Список мобов:*\n\n"
    for (name,) in rows:
        response += f"• {name}\n"
        if len(response) > 3500:
            response += "..."
            break
    await message.answer(response, parse_mode="Markdown")

# --- Команда /mob ---
@dp.message_handler(commands=['mob'])
async def cmd_mob(message: types.Message):
    query = message.get_args().strip()
    if not query:
        await message.answer("❓ Укажите название моба после команды.\nПример: `/mob голем`", parse_mode="Markdown")
        return

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    # Поиск моба по частичному совпадению (без учёта регистра)
    c.execute("SELECT id, name, location, level, description FROM mobs WHERE LOWER(name) LIKE LOWER(?)", (f'%{query}%',))
    mob = c.fetchone()
    if not mob:
        await message.answer(f"❌ Моб по запросу `{query}` не найден.", parse_mode="Markdown")
        conn.close()
        return

    mob_id, name, loc, lvl, desc = mob
    c.execute("SELECT item_name, chance FROM drops WHERE mob_id = ?", (mob_id,))
    drops = c.fetchall()
    conn.close()

    response = f"👾 *{name}*\n"
    response += f"📍 *Локация:* {loc}\n"
    response += f"⭐ *Уровень:* {lvl}\n"
    response += f"📖 *Описание:* {desc}\n\n"
    response += "🎁 *Дроп:*\n"
    if drops:
        for item, chance in drops:
            response += f"• {item} ({chance})\n"
    else:
        response += "Нет информации о дропе.\n"

    await message.answer(response, parse_mode="Markdown")

# --- Обработка текстовых сообщений (поиск по ключевым словам) ---
@dp.message_handler(content_types=types.ContentType.TEXT)
async def text_search(message: types.Message):
    text = message.text.strip()
    if text.startswith('/'):
        return
    # Перенаправляем на команду /mob
    await cmd_mob(message)

# --- Запуск бота ---
async def on_startup(dp):
    init_db()
    load_initial_data()
    logging.info("Бот запущен и готов к работе!")

if __name__ == '__main__':
    executor.start_polling(dp, on_startup=on_startup, skip_updates=True)
