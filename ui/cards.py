from __future__ import annotations

from typing import Protocol

from game_constants import (
    GEAR_SLOT_ICONS,
    GEAR_SLOT_LABELS,
    RARITY_EMOJIS,
    format_gear_classes,
)
from utils import clean_username, escape_html

from .links import (
    EntityLinkBuilder,
    EntityLinkMode,
    MarkupPair,
    combine_markup,
)
from .rich import CardComposer, CardView


DEFAULT_BOT_USERNAME = "fog_database_bot"

RESOURCE_TYPE_NAMES = {
    "craft": "⚒️ Крафтовый",
    "consumable": "✨ Расходуемый",
    "scroll_recipe": "📜 Рецепт экипировки",
    "currency": "💰 Валюта",
    "alchemy": "⚗️ Алхимия",
}

RESOURCE_TYPE_TITLES = {
    "craft": "Крафтовые",
    "consumable": "Расходуемые",
    "scroll_recipe": "Рецепты экипировки",
    "currency": "Валюта",
    "alchemy": "Алхимия",
}

DEFAULT_ALCHEMY_CRAFT_LOCATION = (
    "🏛 Алькасар - 🛣 Вторая улица - 👤 Алхимик - ⚗️ Алхимия"
)
MEREDITH_ALCHEMY_CRAFT_LOCATION = (
    "🏰 Торговый аванпост - 🛣 Центральная Аллея - 👤 Ученая Мередит - ⚗️ Алхимия"
)
MEREDITH_ALCHEMY_RESOURCES = frozenset(
    name.casefold()
    for name in (
        "Дубленая кожа",
        "Костяной куб",
        "Пепельный материал",
        "Прочная бечевка",
        "Субстанция",
        "Ядро земель",
    )
)


class CatalogDatabase(Protocol):
    async def get_mob_full_card(self, mob_id: int): ...
    async def get_resource_card(self, resource_id: int): ...
    async def get_recipe_for_resource(self, resource_id: int): ...
    async def get_gear_card(self, gear_id: int): ...
    async def get_card_by_id(self, card_id: int): ...
    async def get_card_drop_mobs(self, card_id: int): ...


def get_rarity_emoji(rarity: str | None) -> str:
    return RARITY_EMOJIS.get(rarity or "common", RARITY_EMOJIS["common"])


def get_resource_type_name(resource_type: str | None) -> str:
    return RESOURCE_TYPE_NAMES.get(resource_type or "craft", "📦 Крафтовый")


def get_alchemy_craft_location(resource_name: str) -> str:
    if resource_name.strip().casefold() in MEREDITH_ALCHEMY_RESOURCES:
        return MEREDITH_ALCHEMY_CRAFT_LOCATION
    return DEFAULT_ALCHEMY_CRAFT_LOCATION


def build_resource_return_param(
    resource_id: int,
    context_type: str | None,
    context_id: int | str | None,
    page: int,
) -> str | None:
    if context_id is None:
        return None
    if context_type == "location":
        return f"resource_loc_{resource_id}_{context_id}_{page}"
    if context_type == "type":
        return f"resource_type_{resource_id}_{context_id}_{page}"
    return None


def build_gear_return_param(
    gear_id: int,
    rarity: str | None,
    page: int,
) -> str | None:
    return f"gear_{gear_id}_{rarity}_{page}" if rarity else None


def _link_builder(
    bot_username: str | None,
    link_mode: EntityLinkMode,
    source_type: str,
    source_id: int,
) -> EntityLinkBuilder:
    return EntityLinkBuilder(
        bot_username or DEFAULT_BOT_USERNAME,
        link_mode,
        source_type,
        source_id,
    )


def _entity_line(
    links: EntityLinkBuilder,
    *,
    item_type: str,
    item_id: int,
    name: str,
    prefix: str = "",
    suffix: str = "",
    return_param: str | None = None,
) -> MarkupPair:
    link = links.link(
        item_type,
        item_id,
        escape_html(name),
        return_param,
    )
    return combine_markup(prefix, link, suffix)


def build_resource_usage_rows(
    usages: list[dict],
    return_param: str | None,
    links: EntityLinkBuilder,
) -> list[tuple[MarkupPair, int]]:
    rows: list[tuple[MarkupPair, int]] = []
    sorted_usages = sorted(
        usages,
        key=lambda usage: (
            str(usage.get("result_name") or "").casefold(),
            int(usage.get("result_id") or 0),
        ),
    )
    for usage in sorted_usages:
        result_type = usage.get("result_type")
        result_id = usage.get("result_id")
        if result_type not in {"gear", "resource"} or not result_id:
            continue
        visual_parts = (
            [get_rarity_emoji(usage.get("result_rarity"))]
            if result_type == "gear"
            else []
        )
        visual_parts.append(escape_html(usage.get("result_emoji", "")))
        visual = " ".join(part for part in visual_parts if part)
        rows.append((
            _entity_line(
                links,
                item_type=result_type,
                item_id=result_id,
                name=usage.get("result_name", ""),
                prefix=f"{visual} " if visual else "",
                return_param=return_param,
            ),
            int(usage.get("quantity", 1)),
        ))
    return rows


async def build_mob_card(
    database: CatalogDatabase,
    mob_id: int,
    location_id: int | None = None,
    page: int = 1,
    *,
    bot_username: str | None = None,
    link_mode: EntityLinkMode = EntityLinkMode.DEEP_LINK,
) -> CardView:
    data = await database.get_mob_full_card(mob_id)
    if not data:
        return CardView("Моб не найден.", "Моб не найден.")

    links = _link_builder(bot_username, link_mode, "mob", mob_id)
    return_param = f"mob_{mob_id}_{location_id}_{page}" if location_id else None
    loc_str = f"{escape_html(data['loc_emoji'])} {escape_html(data['loc_name'])}"
    composer = CardComposer()
    composer.add(f"<b>{escape_html(data['emoji'])} {escape_html(data['name'])}</b>")
    composer.add_table(
        [
            [f"<b>❤️ HP:</b> {data['hp']}", f"<b>⭐ Опыт:</b> {data['exp']}"],
            [
                f"<b>✨ Пыль:</b> {data['dust_min']}-{data['dust_max']}",
                f"<b>{loc_str}</b>",
            ],
        ],
        fallback_rows=[
            f"❤️ HP: {data['hp']}",
            f"✨ Пыль: {data['dust_min']}-{data['dust_max']}",
            f"⭐ Опыт: {data['exp']}",
            f"📍 Локация: {loc_str}",
        ],
    )

    if data["resource_drops"]:
        composer.add_list(
            "📦 Падает:",
            [
                _entity_line(
                    links,
                    item_type="resource",
                    item_id=item["id"],
                    name=item["name"],
                    prefix=f"{escape_html(item['emoji'])} ",
                    return_param=return_param,
                )
                for item in data["resource_drops"]
            ],
        )
    if data["gear_drops"]:
        composer.add_list(
            "⚔️ Снаряжение:",
            [
                _entity_line(
                    links,
                    item_type="gear",
                    item_id=item["id"],
                    name=item["name"],
                    prefix=(
                        f"{get_rarity_emoji(item.get('rarity'))} "
                        f"{escape_html(item['emoji'])} "
                    ),
                    return_param=return_param,
                )
                for item in data["gear_drops"]
            ],
        )
    if data["card_drops"]:
        composer.add_list(
            "🃏 Карты:",
            [
                _entity_line(
                    links,
                    item_type="card",
                    item_id=item["id"],
                    name=item["name"],
                    prefix=f"{escape_html(item['emoji'])} ",
                    suffix=f" {GEAR_SLOT_ICONS.get(item.get('slot', ''), '')}",
                    return_param=return_param,
                )
                for item in data["card_drops"]
            ],
        )
    return composer.build()


async def build_resource_card(
    database: CatalogDatabase,
    resource_id: int,
    context_type: str | None = None,
    context_id: int | str | None = None,
    page: int = 1,
    *,
    bot_username: str | None = None,
    link_mode: EntityLinkMode = EntityLinkMode.DEEP_LINK,
) -> CardView:
    data = await database.get_resource_card(resource_id)
    if not data:
        return CardView("Ресурс не найден.", "Ресурс не найден.")

    links = _link_builder(bot_username, link_mode, "resource", resource_id)
    return_param = build_resource_return_param(
        resource_id,
        context_type,
        context_id,
        page,
    )
    is_alchemy = data.get("type") == "alchemy"
    composer = CardComposer()
    composer.add(f"<b>{escape_html(data['emoji'])} {escape_html(data['name'])}</b>")
    composer.add(f"🏷 Тип: {get_resource_type_name(data.get('type'))}")

    if data["mobs"]:
        mob_rows = []
        fallback_rows = []
        for mob in data["mobs"]:
            loc = (
                f"{escape_html(mob.get('location_emoji', ''))} "
                f"{escape_html(mob.get('location_name', ''))}"
                if mob.get("location_name")
                else ""
            )
            mob_link = _entity_line(
                links,
                item_type="mob",
                item_id=mob["id"],
                name=mob["name"],
                prefix=f"{escape_html(mob['emoji'])} ",
                return_param=return_param,
            )
            mob_rows.append([mob_link, loc])
            fallback_rows.append(combine_markup(mob_link, f" <i>{loc}</i>"))
        composer.add_table(
            mob_rows,
            headers=["Моб", "Локация"],
            title="Падает с мобов:",
            fallback_rows=fallback_rows,
        )

    usage_rows = build_resource_usage_rows(
        data.get("used_in", []),
        return_param,
        links,
    )
    if usage_rows:
        composer.add_table(
            [[result, f"{quantity} шт."] for result, quantity in usage_rows],
            headers=["Результат", "Нужно"],
            details_summary="🧩 Используется в рецептах:",
            fallback_rows=[
                combine_markup(result, f" — {quantity} шт.")
                for result, quantity in usage_rows
            ],
            fallback_spoiler=True,
        )

    if data.get("note"):
        composer.add(f"📝 <i>{escape_html(data['note'])}</i>")

    recipe = await database.get_recipe_for_resource(resource_id)
    if recipe and recipe["ingredients"]:
        ingredient_rows = []
        fallback_rows = []
        ingredients = [
            ingredient
            for ingredient in recipe["ingredients"]
            if ingredient["resource_id"] == 71
        ] + [
            ingredient
            for ingredient in recipe["ingredients"]
            if ingredient["resource_id"] != 71
        ]
        for ingredient in ingredients:
            is_dust = ingredient["resource_id"] == 71
            label = "Пыль" if is_dust else ingredient["name"]
            prefix = "✨ " if is_dust else f"{escape_html(ingredient['emoji'])} "
            item_link = _entity_line(
                links,
                item_type="resource",
                item_id=ingredient["resource_id"],
                name=label,
                prefix=prefix,
                return_param=return_param,
            )
            quantity = f"{ingredient['quantity']} шт."
            ingredient_rows.append([item_link, quantity])
            fallback_rows.append(combine_markup(item_link, f" — {quantity}"))
        composer.add_table(
            ingredient_rows,
            headers=["Ресурс", "Количество"],
            title=None if is_alchemy else "⚗️ Алхимия:",
            fallback_rows=fallback_rows,
        )
        craft_location = get_alchemy_craft_location(data["name"])
        composer.add(
            f"🏛 <b>Где крафтить:</b><br>{craft_location}",
            f"🏛 <b>Где крафтить:</b>\n{craft_location}",
        )
    return composer.build()


async def build_gear_card(
    database: CatalogDatabase,
    gear_id: int,
    rarity: str | None = None,
    page: int = 1,
    *,
    data: dict | None = None,
    bot_username: str | None = None,
    link_mode: EntityLinkMode = EntityLinkMode.DEEP_LINK,
) -> CardView:
    if data is None:
        data = await database.get_gear_card(gear_id)
    if not data:
        return CardView("Предмет не найден.", "Предмет не найден.")

    links = _link_builder(bot_username, link_mode, "gear", gear_id)
    return_param = build_gear_return_param(gear_id, rarity, page)
    craft_text = "да" if data.get("craftable") else "нет"
    composer = CardComposer()
    composer.add(
        f"<b>{get_rarity_emoji(data.get('rarity'))} "
        f"{escape_html(data['emoji'])} {escape_html(data['name'])}</b>"
    )
    composer.add_table(
        [[
            str(data.get("level", 1)),
            escape_html(format_gear_classes(data.get("classes"))),
            craft_text,
        ]],
        headers=["Уровень", "Класс", "Крафт"],
        fallback_rows=[
            f"Уровень: {data.get('level', 1)}",
            f"Класс: {escape_html(format_gear_classes(data.get('classes')))}",
            f"Крафт: {craft_text}",
        ],
    )
    if data.get("note"):
        composer.add(
            f"📝 <b>Примечание:</b> {escape_html(data['note'])}",
            f"📝 {escape_html(data['note'])}",
        )

    if data.get("craftable"):
        if data["ingredients"]:
            ingredient_rows = []
            fallback_rows = []
            for ingredient in data["ingredients"]:
                item_link = _entity_line(
                    links,
                    item_type="resource",
                    item_id=ingredient["id"],
                    name=ingredient["name"],
                    prefix=f"{escape_html(ingredient['emoji'])} ",
                    return_param=return_param,
                )
                quantity = f"{ingredient['quantity']} шт."
                ingredient_rows.append([item_link, quantity])
                fallback_rows.append(combine_markup(item_link, f" — {quantity}"))
            composer.add_table(
                ingredient_rows,
                title="Требуемые ресурсы:",
                fallback_rows=fallback_rows,
            )
        else:
            composer.add("<i>Рецепт пока не заполнен.</i>")
        if data.get("owners"):
            owners = [
                f"@{escape_html(clean_username(owner))}"
                for owner in data["owners"]
            ]
            composer.add(
                "<details><summary>👥 Владельцы рецепта</summary>"
                + "<br>".join(owners)
                + "</details>",
                "<b>👥 Владельцы рецепта:</b>\n" + "\n".join(owners),
            )

    if data["scroll_mobs"]:
        composer.add_list(
            "📜 Свиток падает с мобов:",
            [
                _entity_line(
                    links,
                    item_type="mob",
                    item_id=mob["id"],
                    name=mob["name"],
                    prefix=f"{escape_html(mob['emoji'])} ",
                    return_param=return_param,
                )
                for mob in data["scroll_mobs"]
            ],
        )
    if data["mobs"]:
        composer.add_list(
            "⚔️ Выпадает с мобов:",
            [
                _entity_line(
                    links,
                    item_type="mob",
                    item_id=mob["id"],
                    name=mob["name"],
                    prefix=f"{escape_html(mob['emoji'])} ",
                    return_param=return_param,
                )
                for mob in data["mobs"]
            ],
        )
    return composer.build()


async def build_card_card(
    database: CatalogDatabase,
    card_id: int,
    page: int = 1,
    context_type: str | None = None,
    context_id: int | str | None = None,
    *,
    bot_username: str | None = None,
    link_mode: EntityLinkMode = EntityLinkMode.DEEP_LINK,
) -> CardView:
    card = await database.get_card_by_id(card_id)
    if not card:
        return CardView("Карта не найдена.", "Карта не найдена.")

    links = _link_builder(bot_username, link_mode, "card", card_id)
    return_param = None
    if context_type and context_id:
        if context_type == "location":
            return_param = f"card_loc_{card_id}_{context_id}_{page}"
        elif context_type == "type":
            return_param = f"card_type_{card_id}_{context_id}_{page}"

    composer = CardComposer()
    composer.add(f"🃏 {escape_html(card['emoji'])} <b>{escape_html(card['name'])}</b>")
    composer.add(f"Слот: {escape_html(GEAR_SLOT_LABELS.get(card['slot'], card['slot']))}")
    bonuses = [card.get(f"bonus{index}", "") for index in range(1, 5)]
    bonuses = [bonus for bonus in bonuses if bonus]
    if bonuses:
        composer.add_list("Бонусы:", [f"• {escape_html(bonus)}" for bonus in bonuses])
    if card.get("note"):
        composer.add(f"📰 <i>{escape_html(card['note'])}</i>")

    mobs = await database.get_card_drop_mobs(card_id)
    if mobs:
        items = []
        for mob in mobs:
            loc = (
                f"{escape_html(mob['location_emoji'])} "
                f"{escape_html(mob['location_name'])}"
                if mob.get("location_name")
                else ""
            )
            items.append(_entity_line(
                links,
                item_type="mob",
                item_id=mob["id"],
                name=mob["name"],
                prefix=f"{escape_html(mob['emoji'])} ",
                suffix=f" <i>{loc}</i>" if loc else "",
                return_param=return_param,
            ))
        composer.add_list("📜 Падает с мобов:", items)
    else:
        composer.add("<i>Нет информации</i>")
    return composer.build()
