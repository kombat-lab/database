from __future__ import annotations

from typing import Protocol

from catalog_types import (
    CardRow,
    GearCardRow,
    MobCardRow,
    ResourceCardRow,
    ResourceRecipeRow,
    ResourceDropMobRow,
    ResourceUsageRow,
    RecipeOwnerEntry,
)
from .callbacks import build_gear_return_param, build_resource_return_param
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


class CatalogDatabase(Protocol):
    async def get_mob_full_card(self, mob_id: int) -> MobCardRow | None: ...
    async def get_resource_card(self, resource_id: int) -> ResourceCardRow | None: ...
    async def get_recipe_for_resource(self, resource_id: int) -> ResourceRecipeRow | None: ...
    async def get_gear_card(self, gear_id: int) -> GearCardRow | None: ...
    async def get_card_by_id(self, card_id: int) -> CardRow | None: ...
    async def get_card_drop_mobs(self, card_id: int) -> list[ResourceDropMobRow]: ...


def get_rarity_emoji(rarity: str | None) -> str:
    return RARITY_EMOJIS.get(rarity or "common", RARITY_EMOJIS["common"])


def get_resource_type_name(resource_type: str | None) -> str:
    return RESOURCE_TYPE_NAMES.get(resource_type or "craft", "📦 Крафтовый")


def format_recipe_owner(owner: RecipeOwnerEntry) -> str:
    username = owner.get("player_username")
    if username:
        return f"@{escape_html(clean_username(username))}"
    user_id = owner.get("user_id")
    if user_id is not None:
        return f'<a href="tg://user?id={user_id}">Пользователь {user_id}</a>'
    return "Без имени"


def _add_recipe_owners(composer: CardComposer, owners: list[str]) -> None:
    if owners:
        composer.add(
            "<details><summary>👥 Кто изучил рецепт</summary>" + "<br>".join(owners) + "</details>",
            "<b>👥 Кто изучил рецепт:</b>\n" + "\n".join(owners),
        )


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
    usages: list[ResourceUsageRow],
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
        visual_parts = [get_rarity_emoji(usage.get("result_rarity"))] if result_type == "gear" else []
        visual_parts.append(escape_html(usage.get("result_emoji", "")))
        visual = " ".join(part for part in visual_parts if part)
        rows.append(
            (
                _entity_line(
                    links,
                    item_type=result_type,
                    item_id=result_id,
                    name=usage.get("result_name", ""),
                    prefix=f"{visual} " if visual else "",
                    return_param=return_param,
                ),
                int(usage.get("quantity", 1)),
            )
        )
    return rows


async def build_mob_card(
    database: CatalogDatabase,
    mob_id: int,
    location_id: int | None = None,
    page: int = 1,
    *,
    data: MobCardRow | None = None,
    bot_username: str | None = None,
    link_mode: EntityLinkMode = EntityLinkMode.DEEP_LINK,
) -> CardView:
    if data is None:
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

    drop_sections = []
    if data["resource_drops"]:
        drop_sections.append(
            (
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
        )
    if data["gear_drops"]:
        drop_sections.append(
            (
                "⚔️ Снаряжение:",
                [
                    _entity_line(
                        links,
                        item_type="gear",
                        item_id=item["id"],
                        name=item["name"],
                        prefix=(f"{get_rarity_emoji(item.get('rarity'))} {escape_html(item['emoji'])} "),
                        return_param=return_param,
                    )
                    for item in data["gear_drops"]
                ],
            )
        )
    if data["card_drops"]:
        drop_sections.append(
            (
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
        )
    for index, (title, items) in enumerate(drop_sections):
        if index:
            composer.add_divider()
        composer.add_list(title, items)
    return composer.build()


async def build_resource_card(
    database: CatalogDatabase,
    resource_id: int,
    context_type: str | None = None,
    context_id: int | str | None = None,
    page: int = 1,
    *,
    data: ResourceCardRow | None = None,
    bot_username: str | None = None,
    link_mode: EntityLinkMode = EntityLinkMode.DEEP_LINK,
) -> CardView:
    if data is None:
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

    is_learning_scroll = data.get("type") == "scroll_recipe"
    if is_learning_scroll:
        composer.add("📖 <b>Изучить рецепт один раз</b>")
        for learned_recipe in data.get("learning_recipes", []):
            result_link = _entity_line(
                links,
                item_type="gear",
                item_id=learned_recipe["result_id"],
                name=learned_recipe["result_name"],
                prefix=f"{get_rarity_emoji(learned_recipe['result_rarity'])} {escape_html(learned_recipe['result_emoji'])} ",
                return_param=return_param,
            )
            composer.add_pair(combine_markup("Результат: ", result_link, f" × {learned_recipe['quantity']} шт."))
            material_rows: list[list[MarkupPair | str]] = []
            for material in learned_recipe["ingredients"]:
                material_link = _entity_line(
                    links,
                    item_type="resource",
                    item_id=material["resource_id"],
                    name=material["name"],
                    prefix=f"{escape_html(material['emoji'])} ",
                    return_param=return_param,
                )
                material_rows.append([material_link, f"{material['quantity']} шт."])
            if material_rows:
                composer.add_table(material_rows, title="Материалы на один крафт:")
            else:
                composer.add("<i>Материалы рецепта пока не заполнены.</i>")
            _add_recipe_owners(
                composer,
                [format_recipe_owner(owner) for owner in learned_recipe["owner_entries"]],
            )

    if data["mobs"]:
        mob_rows: list[list[MarkupPair | str]] = []
        fallback_rows = []
        for mob in data["mobs"]:
            loc = (
                f"{escape_html(mob.get('location_emoji', ''))} {escape_html(mob.get('location_name', ''))}"
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
            fallback_rows=[combine_markup(result, f" — {quantity} шт.") for result, quantity in usage_rows],
            fallback_spoiler=True,
        )

    if data.get("note"):
        composer.add(f"📝 <i>{escape_html(data['note'])}</i>")

    recipe = None if is_learning_scroll else await database.get_recipe_for_resource(resource_id)
    if recipe and recipe["ingredients"]:
        composer.add(f"За один крафт: {recipe.get('quantity', 1)} шт.")
        ingredient_rows: list[list[MarkupPair | str]] = []
        fallback_rows = []
        ingredients = [ingredient for ingredient in recipe["ingredients"] if ingredient.get("code") == "dust"] + [
            ingredient for ingredient in recipe["ingredients"] if ingredient.get("code") != "dust"
        ]
        for ingredient in ingredients:
            is_dust = ingredient.get("code") == "dust"
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
    craft_location = data.get("craft_location")
    if craft_location and not is_learning_scroll:
        composer.add(
            f"🏛 <b>Где крафтить:</b><br>{escape_html(craft_location)}",
            f"🏛 <b>Где крафтить:</b>\n{escape_html(craft_location)}",
        )
    return composer.build()


async def build_gear_card(
    database: CatalogDatabase,
    gear_id: int,
    rarity: str | None = None,
    page: int = 1,
    *,
    data: GearCardRow | None = None,
    slot_index: int | None = None,
    bot_username: str | None = None,
    link_mode: EntityLinkMode = EntityLinkMode.DEEP_LINK,
) -> CardView:
    if data is None:
        data = await database.get_gear_card(gear_id)
    if not data:
        return CardView("Предмет не найден.", "Предмет не найден.")

    gear_id = data["id"]
    rarity = data["rarity"]
    links = _link_builder(bot_username, link_mode, "gear", gear_id)
    return_param = build_gear_return_param(gear_id, rarity, page, slot_index)
    craft_text = "да" if data.get("craftable") else "нет"
    composer = CardComposer()
    composer.add(
        f"<b>{get_rarity_emoji(data.get('rarity'))} {escape_html(data['emoji'])} {escape_html(data['name'])}</b>"
    )
    composer.add_table(
        [
            [
                str(data.get("level", 1)),
                escape_html(format_gear_classes(data.get("classes"))),
                craft_text,
            ]
        ],
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
        composer.add(f"За один крафт: {data.get('craft_quantity', 1)} шт.")
        learning_scroll = data.get("learning_scroll")
        if learning_scroll:
            composer.add_list(
                "📖 Изучить рецепт один раз:",
                [
                    _entity_line(
                        links,
                        item_type="resource",
                        item_id=learning_scroll["id"],
                        name=learning_scroll["name"],
                        prefix=f"{escape_html(learning_scroll['emoji'])} ",
                        return_param=return_param,
                    ),
                ],
            )
        if data["ingredients"]:
            ingredient_rows: list[list[MarkupPair | str]] = []
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
                title="Материалы на один крафт:",
                fallback_rows=fallback_rows,
            )
        else:
            composer.add("<i>Рецепт пока не заполнен.</i>")
        owners = (
            [format_recipe_owner(owner) for owner in data["owner_entries"]]
            if "owner_entries" in data
            else [f"@{escape_html(clean_username(owner))}" for owner in data.get("owners", [])]
        )
        _add_recipe_owners(composer, owners)

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
    data: CardRow | None = None,
    bot_username: str | None = None,
    link_mode: EntityLinkMode = EntityLinkMode.DEEP_LINK,
) -> CardView:
    card = data if data is not None else await database.get_card_by_id(card_id)
    if not card:
        return CardView("Карта не найдена.", "Карта не найдена.")

    links = _link_builder(bot_username, link_mode, "card", card_id)
    return_param = f"card_{card_id}_{page}"
    if context_type and context_id:
        if context_type == "location":
            return_param = f"card_loc_{card_id}_{context_id}_{page}"
        elif context_type == "type":
            return_param = f"card_type_{card_id}_{context_id}_{page}"

    composer = CardComposer()
    composer.add(f"🃏 {escape_html(card['emoji'])} <b>{escape_html(card['name'])}</b>")
    composer.add(f"Слот: {escape_html(GEAR_SLOT_LABELS.get(card['slot'], card['slot']))}")
    bonuses = [card["bonus1"], card["bonus2"], card["bonus3"], card["bonus4"]]
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
                f"{escape_html(mob['location_emoji'])} {escape_html(mob['location_name'])}"
                if mob.get("location_name")
                else ""
            )
            items.append(
                _entity_line(
                    links,
                    item_type="mob",
                    item_id=mob["id"],
                    name=mob["name"],
                    prefix=f"{escape_html(mob['emoji'])} ",
                    suffix=f" <i>{loc}</i>" if loc else "",
                    return_param=return_param,
                )
            )
        composer.add_list("📜 Падает с мобов:", items)
    else:
        composer.add("<i>Нет информации</i>")
    return composer.build()
