# stats_handlers.py
import logging
import asyncio
from aiogram import Router, types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from database import db
from analytics import (
    get_active_users_count,
    get_retention,
    get_top_items_with_names,
    get_db_stats,
    reset_analytics_data,
    get_top_search_queries,
)

logger = logging.getLogger(__name__)

stats_router = Router()

# ------------------------------------------------------------
async def show_stats_menu(target, edit: bool = False):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🐾 Топ-30 мобов", callback_data="stats_mobs")],
        [InlineKeyboardButton(text="📦 Топ-30 ресурсов", callback_data="stats_resources")],
        [InlineKeyboardButton(text="⚔️ Топ-30 снаряжения", callback_data="stats_gear")],
        [InlineKeyboardButton(text="🃏 Топ-30 карт", callback_data="stats_cards")],
        [InlineKeyboardButton(text="🔍 Топ-30 поисковых запросов", callback_data="stats_searches")],
        [InlineKeyboardButton(text="📊 Общая статистика", callback_data="stats_general")],
        [InlineKeyboardButton(text="🗑 Сбросить аналитику", callback_data="stats_reset")],
        [InlineKeyboardButton(text="🔙 Назад в админку", callback_data="admin_cancel_edit")]
    ])
    if edit and isinstance(target, types.CallbackQuery):
        await target.message.edit_text("📈 Выберите раздел статистики:", reply_markup=keyboard)
    else:
        await target.answer("📈 Выберите раздел статистики:", reply_markup=keyboard)

async def show_top_items(callback: types.CallbackQuery, item_type: str, type_name_ru: str):
    items = await get_top_items_with_names(item_type, days=30, limit=30)
    if not items:
        text = f"📊 Нет данных по {type_name_ru} за последние 30 дней."
    else:
        lines = []
        for idx, item in enumerate(items, 1):
            emoji = item.get('emoji', '')
            name = item.get('name', f"ID {item['target_id']}")
            views = item['views']
            lines.append(f"{idx}. {emoji} {name} — {views} просмотров")
        text = f"🏆 <b>Топ-30 {type_name_ru} за 30 дней</b>\n\n" + "\n".join(lines)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад к статистике", callback_data="back_to_stats")]
    ])
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
    await callback.answer()

async def show_top_searches(callback: types.CallbackQuery):
    items = await get_top_search_queries(days=30, limit=30, search_type='all')
    if not items:
        text = "📊 Нет поисковых запросов за последние 30 дней."
    else:
        lines = []
        for idx, item in enumerate(items, 1):
            query = item['query'][:50]
            count = item['count']
            lines.append(f"{idx}. \"{query}\" — {count} раз")
        text = f"🔍 <b>Топ-30 поисковых запросов за 30 дней</b>\n\n" + "\n".join(lines)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад к статистике", callback_data="back_to_stats")]
    ])
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
    await callback.answer()

async def show_general_stats(callback: types.CallbackQuery):
    dau = await get_active_users_count(1)
    wau = await get_active_users_count(7)
    mau = await get_active_users_count(30)
    retention_d1 = await get_retention(1, 1)
    retention_d7 = await get_retention(7, 7)
    retention_d30 = await get_retention(30, 30)
    db_stats = await get_db_stats()
    db_size_mb = db_stats['db_size_bytes'] / (1024 * 1024)
    text = (
        f"📊 <b>Общая статистика бота</b>\n\n"
        f"👥 <b>Активные пользователи</b>\n"
        f"  • За день (DAU): {dau}\n"
        f"  • За неделю (WAU): {wau}\n"
        f"  • За месяц (MAU): {mau}\n\n"
        f"🔄 <b>Удержание (Retention)</b>\n"
        f"  • День 1: {retention_d1:.1f}%\n"
        f"  • Неделя 1: {retention_d7:.1f}%\n"
        f"  • Месяц 1: {retention_d30:.1f}%\n\n"
        f"💾 <b>Состояние БД</b>\n"
        f"  • Событий: {db_stats['events']}\n"
        f"  • Пользователей: {db_stats['users']}\n"
        f"  • Размер: {db_size_mb:.2f} МБ"
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад к статистике", callback_data="back_to_stats")]
    ])
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
    await callback.answer()

async def confirm_reset(callback: types.CallbackQuery):
    """Показывает подтверждение сброса, только если текущее сообщение не является уже формой подтверждения."""
    # Проверяем, не находится ли уже пользователь в режиме подтверждения
    current_text = callback.message.text or ""
    if "⚠️ ВНИМАНИЕ!" in current_text:
        # Уже показываем подтверждение — ничего не делаем
        await callback.answer("Подтверждение уже показано")
        return
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚠️ ДА, СБРОСИТЬ ВСЁ", callback_data="stats_reset_confirm")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="back_to_stats")]
    ])
    try:
        await callback.message.edit_text(
            "⚠️ <b>ВНИМАНИЕ!</b>\n\n"
            "Вы собираетесь полностью удалить ВСЮ аналитику:\n"
            "- историю просмотров карточек\n"
            "- данные о пользователях\n"
            "- все события\n\n"
            "<b>Это действие необратимо!</b>\n\n"
            "Уверены?",
            parse_mode="HTML",
            reply_markup=keyboard
        )
    except types.TelegramBadRequest as e:
        if "message is not modified" in str(e):
            # Игнорируем, если сообщение не изменилось
            pass
        else:
            raise
    await callback.answer()

# ------------------------------------------------------------
# Хендлеры
# ------------------------------------------------------------
@stats_router.message(Command("stats"))
async def show_stats_command(message: types.Message):
    from admin_handlers import is_admin
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Нет доступа.")
        return
    await show_stats_menu(message)

@stats_router.callback_query(F.data == "admin_stats")
async def admin_stats_callback(callback: types.CallbackQuery):
    await show_stats_menu(callback, edit=True)
    await callback.answer()

@stats_router.callback_query(F.data.startswith("stats_"))
async def stats_router_callback(callback: types.CallbackQuery):
    action = callback.data.split("_")[1]
    if action == "mobs":
        await show_top_items(callback, 'mob', 'мобов')
    elif action == "resources":
        await show_top_items(callback, 'resource', 'ресурсов')
    elif action == "gear":
        await show_top_items(callback, 'gear', 'предметов снаряжения')
    elif action == "cards":
        await show_top_items(callback, 'card', 'карт')
    elif action == "searches":
        await show_top_searches(callback)
    elif action == "general":
        await show_general_stats(callback)
    elif action == "reset":
        await confirm_reset(callback)
    else:
        await callback.answer("Неизвестная команда")

@stats_router.callback_query(F.data == "stats_reset_confirm")
async def reset_analytics_callback(callback: types.CallbackQuery):
    """Выполняет сброс аналитических таблиц."""
    # Показываем "песочные часы"
    await callback.message.edit_text("⏳ Сброс аналитики...")
    try:
        await reset_analytics_data()
        # После сброса принудительно обновляем кэш пользователей, если он есть (у вас его нет)
        # Но можно принудительно перечитать статистику (например, показать пустые значения)
        await callback.message.edit_text("✅ Аналитика полностью сброшена.")
    except Exception as e:
        logger.exception("Ошибка сброса аналитики")
        await callback.message.edit_text(f"❌ Ошибка при сбросе: {e}")
    # Возвращаем в меню статистики
    await asyncio.sleep(1)
    await show_stats_menu(callback.message, edit=False)
    await callback.answer()
