import os
import logging
import json
import redis.asyncio as aioredis
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, types
from aiogram.contrib.middlewares.logging import LoggingMiddleware
from aiogram.utils import executor

# Загружаем переменные окружения из файла .env (для локальной разработки)
load_dotenv()

# --- Переменные окружения ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

# Redis
REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", 6379))
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD", "")
REDIS_DB = int(os.getenv("REDIS_DB", 0))

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

# --- Функция подключения к Redis ---
async def get_redis():
    return await aioredis.from_url(
        f"redis://:{REDIS_PASSWORD}@{REDIS_HOST}:{REDIS_PORT}/{REDIS_DB}",
        decode_responses=True
    )

# --- Загрузка тестовых данных (при первом запуске) ---
async def load_initial_data():
    redis = await get_redis()
    # Проверяем, есть ли уже индекс мобов
    if await redis.exists("mob_index"):
        logging.info("Данные уже загружены в Redis, пропускаем инициализацию.")
        await redis.close()
        return

    logging.info("Загрузка начальных данных в Redis...")
    test_mobs = {
        1: {
            "name": "Лесной голем",
            "location": "Тёмный лес",
            "level": 15,
            "description": "Огромное каменное существо, охраняющее древние руины.",
            "drops": [
                {"item_name": "древесный уголь", "chance": "100%"},
                {"item_name": "камень души", "chance": "5%"}
            ]
        },
        2: {
            "name": "Огненный слизень",
            "location": "Пепельные земли",
            "level": 22,
            "description": "Раскалённая субстанция, оставляющая за собой след из лавы.",
            "drops": [
                {"item_name": "огненная слизь", "chance": "80%"},
                {"item_name": "искра магии", "chance": "30%"}
            ]
        }
    }

    for mob_id, mob_data in test_mobs.items():
        # Сохраняем карточку моба (Hash)
        await redis.hset(f"mob:{mob_id}", mapping={
            "name": mob_data["name"],
            "location": mob_data["location"],
            "level": mob_data["level"],
            "description": mob_data["description"]
        })
        # Сохраняем дроп (JSON строка)
        await redis.set(f"mob_drops:{mob_id}", json.dumps(mob_data["drops"], ensure_ascii=False))
        # Добавляем имя в индекс для поиска
        await redis.sadd("mob_index", mob_data["name"])
        # Сохраняем связь имя -> ID (для быстрого поиска)
        await redis.set(f"mob_name_to_id:{mob_data['name']}", mob_id)

    logging.info("Тестовые данные загружены.")
    await redis.close()

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
    redis = await get_redis()
    mob_names = []
    async for name in redis.sscan_iter("mob_index"):
        mob_names.append(name)
    await redis.close()

    if not mob_names:
        await message.answer("Список мобов пуст.")
        return

    # Формируем красивое сообщение
    response = "📋 *Список мобов:*\n\n"
    for name in sorted(mob_names):
        response += f"• {name}\n"
        if len(response) > 3500:  # Telegram ограничение
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

    redis = await get_redis()
    # Ищем моба по индексу
    found_mobs = []
    async for mob_name in redis.sscan_iter("mob_index"):
        if query.lower() in mob_name.lower():
            found_mobs.append(mob_name)

    if not found_mobs:
        await message.answer(f"❌ Моб по запросу `{query}` не найден.", parse_mode="Markdown")
        await redis.close()
        return

    # Берём первого подходящего
    mob_name = found_mobs[0]

    # Получаем ID моба по имени
    mob_id = await redis.get(f"mob_name_to_id:{mob_name}")
    if not mob_id:
        await message.answer(f"❌ Ошибка: не найден ID для моба `{mob_name}`.", parse_mode="Markdown")
        await redis.close()
        return

    # Получаем карточку моба
    mob_data = await redis.hgetall(f"mob:{mob_id}")
    if not mob_data:
        await message.answer("❌ Данные моба не найдены.")
        await redis.close()
        return

    # Получаем дроп
    drops_json = await redis.get(f"mob_drops:{mob_id}")
    drops = json.loads(drops_json) if drops_json else []

    # Формируем ответ
    response = f"👾 *{mob_data['name']}*\n"
    response += f"📍 *Локация:* {mob_data['location']}\n"
    response += f"⭐ *Уровень:* {mob_data['level']}\n"
    response += f"📖 *Описание:* {mob_data['description']}\n\n"
    response += "🎁 *Дроп:*\n"
    if drops:
        for drop in drops:
            response += f"• {drop['item_name']} ({drop['chance']})\n"
    else:
        response += "Нет информации о дропе.\n"

    await message.answer(response, parse_mode="Markdown")
    await redis.close()

# --- Обработка текстовых сообщений (поиск по ключевым словам) ---
@dp.message_handler(content_types=types.ContentType.TEXT)
async def text_search(message: types.Message):
    # Если сообщение не начинается с /, пробуем поискать моба
    text = message.text.strip()
    if text.startswith('/'):
        return
    # Эмулируем команду /mob
    await cmd_mob(message)

# --- Запуск бота ---
async def on_startup(dp):
    await load_initial_data()
    logging.info("Бот запущен и готов к работе!")

if __name__ == '__main__':
    executor.start_polling(dp, on_startup=on_startup, skip_updates=True)
