# stats_handlers.py
import logging
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
)

logger = logging.getLogger(__name__)

stats_router = Router()

# ------------------------------------------------------------
# Вспомогательные функции (клавиатуры и отображение)
# ------------------------------------------------------------

async def show_stats_menu(target, edit: bool = False):
    """Показывает меню статистики с кнопками по типам сущностей."""
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🐾 Топ-30 мобов", callback_data="stats_mobs")],
        [InlineKeyboardButton(text="📦 Топ-30 ресурсов", callback_data="stats_resources")],
        [InlineKeyboardButton(text="⚔️ Топ-30 снаряжения", callback_data="stats_gear")],
        [InlineKeyboardButton(text="🃏 Топ-30 карт", callback_data="stats_cards")],
        [InlineKeyboardButton(text="📊 Общая статистика", callback_data="stats_general")],
        [InlineKeyboardButton(text="🗑 Сбросить аналитику", callback_data="stats_reset")],
        [InlineKeyboardButton(text="🔙 Назад в админку", callback_data="admin_cancel_edit")]
    ])
    if edit and isinstance(target, types.CallbackQuery):
        await target.message.edit_text("📈 Выберите раздел статистики:", reply_markup=keyboard)
    else:
        await target.answer("📈 Выберите раздел статистики:", reply_markup=keyboard)

async def show_top_items(callback: types.CallbackQuery, item_type: str):
    """Показывает топ-30 сущностей указанного типа с названиями и эмодзи."""
    items = await get_top_items_with_names(item_type, days=30, limit=30)
    if not items:
        text = f"📊 Нет данных по {item_type} за последние 30 дней."
    else:
        lines = []
        for idx, item in enumerate(items, 1):
            emoji = item.get('emoji', '')
            name = item.get('name', f"ID {item['target_id']}")
            views = item['views']
            lines.append(f"{idx}. {emoji} {name} — {views} просмотров")
        text = f"🏆 <b>Топ-30 {item_type}ов за 30 дней</b>\n\n" + "\n".join(lines)
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад к статистике", callback_data="back_to_stats")]
    ])
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
    await callback.answer()

async def show_general_stats(callback: types.CallbackQuery):
    """Общая статистика: DAU, WAU, MAU, удержание, размер БД."""
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
    """Подтверждение сброса аналитики."""
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚠️ ДА, СБРОСИТЬ ВСЁ", callback_data="stats_reset_confirm")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="back_to_stats")]
    ])
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
    await callback.answer()

# ------------------------------------------------------------
# Хендлеры команд и callback'ов
# ------------------------------------------------------------

@stats_router.message(Command("stats"))
async def show_stats_command(message: types.Message):
    """Обработчик команды /stats (только для админов)."""
    from admin_handlers import is_admin  # избегаем циклического импорта
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Нет доступа.")
        return
    await show_stats_menu(message)

@stats_router.callback_query(F.data == "admin_stats")
async def admin_stats_callback(callback: types.CallbackQuery):
    """Кнопка "Статистика" в админ-панели."""
    await show_stats_menu(callback, edit=True)
    await callback.answer()

@stats_router.callback_query(F.data.startswith("stats_"))
async def stats_router_callback(callback: types.CallbackQuery):
    """Маршрутизация выбора раздела статистики."""
    action = callback.data.split("_")[1]
    
    if action == "mobs":
        await show_top_items(callback, 'mob')
    elif action == "resources":
        await show_top_items(callback, 'resource')
    elif action == "gear":
        await show_top_items(callback, 'gear')
    elif action == "cards":
        await show_top_items(callback, 'card')
    elif action == "general":
        await show_general_stats(callback)
    elif action == "reset":
        await confirm_reset(callback)
    else:
        await callback.answer("Неизвестная команда")

@stats_router.callback_query(F.data == "stats_reset_confirm")
async def reset_analytics_callback(callback: types.CallbackQuery):
    """Выполняет сброс аналитических таблиц."""
    try:
        await reset_analytics_data()
        await callback.message.edit_text("✅ Аналитика полностью сброшена.")
    except Exception as e:
        await callback.message.edit_text(f"❌ Ошибка при сбросе: {e}")
    # Возвращаем в меню статистики
    await show_stats_menu(callback.message, edit=False)
    await callback.answer()

@stats_router.callback_query(F.data == "back_to_stats")
async def back_to_stats(callback: types.CallbackQuery):
    """Возврат в главное меню статистики."""
    await show_stats_menu(callback, edit=True)
    await callback.answer()
