"""Typed, markup-safe presentation of bounded catalog search results."""

from collections.abc import Mapping, Sequence
from catalog_types import SearchItem as SearchItem

from aiogram.utils.formatting import Bold, Italic, Text, TextLink

from game_constants import RARITY_EMOJIS


SearchResults = Mapping[str, Sequence[SearchItem]]
SEARCH_GROUPS = (
    ("mobs", "mob", "Мобы"),
    ("resources", "resource", "Ресурсы"),
    ("gear", "gear", "Снаряжение"),
    ("cards", "card", "Карты"),
)


def build_search_content(results: SearchResults, bot_username: str | None) -> Text:
    nodes: list[Text | str] = [Bold("🔎 Результаты поиска:"), "\n\n"]
    for category, item_type, title in SEARCH_GROUPS:
        items = results.get(category, ())
        if not items:
            continue
        nodes.extend((Bold(f"{title}:"), "\n"))
        for item in items:
            name: Text = Text(item["name"])
            if bot_username:
                name = TextLink(item["name"], url=f"https://t.me/{bot_username}?start={item_type}_{item['id']}")
            nodes.append(Text(item["emoji"], " ", name))
            if category == "mobs" and item.get("location_name"):
                nodes.append(Italic(" ", item.get("location_emoji") or "", " ", item["location_name"]))
            elif category == "gear":
                nodes.append(Text(" ", RARITY_EMOJIS.get(item.get("rarity", "common"), "")))
            elif category == "cards":
                nodes.append(Text(" (слот: ", item.get("slot", ""), ")"))
            nodes.append("\n")
        if len(items) >= 50:
            nodes.append(Italic("Показаны первые 50 совпадений. Уточни запрос для остальных."))
            nodes.append("\n")
        nodes.append("\n")
    return Text(*nodes)
