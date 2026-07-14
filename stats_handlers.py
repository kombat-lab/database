import logging
from aiogram import Router, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from analytics import (
    get_active_users_count,
    get_retention,
    get_top_items_with_names,
    get_db_stats,
    reset_analytics_data,
    get_top_search_queries,
)
from admin_utils import edit_admin_rich
from utils import escape_html

logger = logging.getLogger(__name__)
stats_router = Router()


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
    text = "📈 Выберите раздел статистики:"
    if edit and isinstance(target, types.CallbackQuery):
        await target.message.edit_text(text, reply_markup=keyboard)
    else:
        await target.answer(text, reply_markup=keyboard)


async def show_top_items(callback: types.CallbackQuery, item_type: str, type_name_ru: str):
    await callback.answer()
    items = await get_top_items_with_names(item_type, days=30, limit=30)
    if not items:
        text = f"📊 Нет данных по {type_name_ru} за последние 30 дней."
        rich_html = text
    else:
        lines = []
        rows = []
        for idx, item in enumerate(items, 1):
            emoji = escape_html(item.get('emoji', ''))
            name = escape_html(item.get('name', f"ID {item['target_id']}"))
            views = item['views']
            lines.append(f"{idx}. {emoji} {name} — {views} просмотров")
            rows.append(f"<tr><td>{idx}</td><td>{emoji} {name}</td><td>{views}</td></tr>")
        text = f"🏆 <b>Топ-30 {type_name_ru} за 30 дней</b>\n\n" + "\n".join(lines)
        rich_html = (
            f"<b>🏆 Топ-30 {type_name_ru} за 30 дней</b><br>"
            "<table><tbody><tr><th>№</th><th>Объект</th><th>Просмотры</th></tr>"
            + "".join(rows) + "</tbody></table>"
        )

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад к статистике", callback_data="back_to_stats")]
    ])
    await edit_admin_rich(callback, rich_html, keyboard, fallback_html=text)


async def show_top_searches(callback: types.CallbackQuery):
    await callback.answer()
    items = await get_top_search_queries(days=30, limit=30, search_type='all')
    if not items:
        text = "📊 Нет поисковых запросов за последние 30 дней."
    else:
        lines, rows = [], []
        for idx, item in enumerate(items, 1):
            query = escape_html((item.get('query') or '')[:50])
            count = item['count']
            lines.append(f"{idx}. «{query}» — {count} раз")
            rows.append(f"<tr><td>{idx}</td><td>{query}</td><td>{count}</td></tr>")
        text = f"🔍 <b>Топ-30 поисковых запросов за 30 дней</b>\n\n" + "\n".join(lines)
        rich_html = (
            "<b>🔍 Топ-30 поисковых запросов за 30 дней</b><br>"
            "<table><tbody><tr><th>№</th><th>Запрос</th><th>Количество</th></tr>"
            + "".join(rows) + "</tbody></table>"
        )
    if not items:
        rich_html = text

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад к статистике", callback_data="back_to_stats")]
    ])
    await edit_admin_rich(callback, rich_html, keyboard, fallback_html=text)


async def show_general_stats(callback: types.CallbackQuery):
    await callback.answer()
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
    rich_html = f"""
    <b>📊 Общая статистика бота</b>
    <table><tbody>
      <tr><th>Показатель</th><th>Значение</th></tr>
      <tr><td>DAU</td><td>{dau}</td></tr>
      <tr><td>WAU</td><td>{wau}</td></tr>
      <tr><td>MAU</td><td>{mau}</td></tr>
      <tr><td>Retention D1</td><td>{retention_d1:.1f}%</td></tr>
      <tr><td>Retention D7</td><td>{retention_d7:.1f}%</td></tr>
      <tr><td>Retention D30</td><td>{retention_d30:.1f}%</td></tr>
      <tr><td>События</td><td>{db_stats['events']}</td></tr>
      <tr><td>Пользователи</td><td>{db_stats['users']}</td></tr>
      <tr><td>Размер БД</td><td>{db_size_mb:.2f} МБ</td></tr>
    </tbody></table>
    """
    await edit_admin_rich(callback, rich_html.strip(), keyboard, fallback_html=text)


@stats_router.callback_query(F.data == "stats_reset")
async def confirm_reset(callback: types.CallbackQuery):
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


@stats_router.callback_query(F.data == "stats_reset_confirm")
async def reset_analytics_callback(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    status_msg = callback.message
    await status_msg.edit_text("⏳ Сбрасываю аналитику...")
    back_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад к статистике", callback_data="back_to_stats")]
    ])

    try:
        # Очищаем состояние FSM, чтобы избежать блокировок
        await state.clear()

        # Вызываем функцию сброса (она сама управляет транзакциями)
        await reset_analytics_data()

        await status_msg.edit_text("✅ Аналитика полностью сброшена.", reply_markup=back_keyboard)
    except Exception as e:
        logger.exception("Сброс аналитики не удался")
        await status_msg.edit_text(
            f"❌ Ошибка при сбросе: {e}\n\n"
            "Возможные причины:\n"
            "• База данных временно заблокирована (попробуйте позже)\n"
            "• Недостаточно прав на выполнение VACUUM",
            reply_markup=back_keyboard,
        )


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


@stats_router.callback_query(F.data == "back_to_stats")
async def back_to_stats(callback: types.CallbackQuery):
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
