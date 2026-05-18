import os
import logging
import sqlite3
from pathlib import Path
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, types
from aiogram.contrib.middlewares.logging import LoggingMiddleware
from aiogram.utils import executor

# Загружаем переменные окружения из .env (для локальной разработки)
load_dotenv()

# --- Переменные окружения ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

# Проверка обязательных переменных
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не задан в переменных окружения")

if ADMIN_ID == 0:
    logging.warning("⚠️ ADMIN_ID не задан. Административные команды отключены.")
else:
    logging.info(f"✅ ADMIN_ID установлен: {ADMIN_ID}")

# --- Настройка хранения данных (для Bothost) ---
DATA_DIR = Path(os.getenv("DATA_DIR", "/app/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "game.db"

# --- Инициализация бота ---
logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot)
dp.middleware.setup(LoggingMiddleware())

# --- Работа с базой данных ---
def get_db_connection():
    """Возвращает соединение с SQLite."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row  # чтобы обращаться по именам колонок
    return conn

def init_db():
    """Создаёт таблицы, если их нет."""
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS mobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            location TEXT,
            level INTEGER,
            description TEXT
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS drops (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mob_id INTEGER NOT NULL,
            item_name TEXT NOT NULL,
            chance TEXT,
            FOREIGN KEY (mob_id) REFERENCES mobs (id) ON DELETE CASCADE
        )
    ''')
    conn.commit()
    conn.close()
    logging.info(f"База данных инициализирована: {DB_PATH}")

def load_initial_data():
    """Заполняет базу тестовыми данными, если она пуста."""
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM mobs")
    count = c.fetchone()[0]
    if count > 0:
        conn.close()
        return
    # Тестовые мобы
    test_mobs = [
        ("Лесной голем", "Тёмный лес", 15,
         "Огромное каменное существо, охраняющее древние руины.",
         [("древесный уголь", "100%"), ("камень души", "5%")]),
        ("Огненный слизень", "Пепельные земли", 22,
         "Раскалённая субстанция, оставляющая за собой след из лавы.",
         [("огненная слизь", "80%"), ("искра магии", "30%")]),
        ("Болотный вурдалак", "Топь", 18,
         "Медлительный, но очень живучий противник.",
         [("гнилая плоть", "90%"), ("болотный жемчуг", "10%")])
    ]
    for name, loc, lvl, desc, drops in test_mobs:
        c.execute(
            "INSERT INTO mobs (name, location, level, description) VALUES (?,?,?,?)",
            (name, loc, lvl, desc)
        )
        mob_id = c.lastrowid
        for item_name, chance in drops:
            c.execute(
                "INSERT INTO drops (mob_id, item_name, chance) VALUES (?,?,?)",
                (mob_id, item_name, chance)
            )
    conn.commit()
    conn.close()
    logging.info("Тестовые данные загружены в базу.")

# --- Команды бота ---
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

@dp.message_handler(commands=['help'])
async def cmd_help(message: types.Message):
    await message.reply(
        "📖 *Справка*\n\n"
        "/mob <название> - поиск моба по части названия\n"
        "/list_mobs - список всех мобов\n\n"
        "Скоро появятся разделы: предметы, крафт, локации.",
        parse_mode="Markdown"
    )

@dp.message_handler(commands=['list_mobs'])
async def cmd_list_mobs(message: types.Message):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT name FROM mobs ORDER BY name")
    rows = c.fetchall()
    conn.close()
    if not rows:
        await message.answer("Список мобов пуст.")
        return
    response = "📋 *Список мобов:*\n\n"
    for row in rows:
        response += f"• {row['name']}\n"
        if len(response) > 3500:
            response += "..."
            break
    await message.answer(response, parse_mode="Markdown")

@dp.message_handler(commands=['mob'])
async def cmd_mob(message: types.Message):
    query = message.get_args().strip()
    if not query:
        await message.answer(
            "❓ Укажите название моба после команды.\nПример: `/mob голем`",
            parse_mode="Markdown"
        )
        return
    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        "SELECT id, name, location, level, description FROM mobs WHERE LOWER(name) LIKE LOWER(?)",
        (f'%{query}%',)
    )
    mob = c.fetchone()
    if not mob:
        await message.answer(f"❌ Моб по запросу `{query}` не найден.", parse_mode="Markdown")
        conn.close()
        return
    c.execute("SELECT item_name, chance FROM drops WHERE mob_id = ?", (mob['id'],))
    drops = c.fetchall()
    conn.close()
    response = f"👾 *{mob['name']}*\n"
    response += f"📍 *Локация:* {mob['location']}\n"
    response += f"⭐ *Уровень:* {mob['level']}\n"
    response += f"📖 *Описание:* {mob['description']}\n\n"
    response += "🎁 *Дроп:*\n"
    if drops:
        for drop in drops:
            response += f"• {drop['item_name']} ({drop['chance']})\n"
    else:
        response += "Нет информации о дропе.\n"
    await message.answer(response, parse_mode="Markdown")

@dp.message_handler(content_types=types.ContentType.TEXT)
async def text_search(message: types.Message):
    """Если пользователь просто пишет текст (не команду) — ищем моба."""
    text = message.text.strip()
    if text.startswith('/'):
        return
    await cmd_mob(message)

# --- Запуск ---
async def on_startup(dp):
    init_db()
    load_initial_data()
    logging.info("✅ Бот успешно запущен!")

if __name__ == '__main__':
    executor.start_polling(dp, on_startup=on_startup, skip_updates=True)
