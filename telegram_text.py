"""Bounded Telegram text with explicit entities and UTF-16-safe boundaries."""

from collections.abc import Sequence
from dataclasses import dataclass
from html.parser import HTMLParser

from aiogram.types import MessageEntity
from aiogram.utils.formatting import Text
from aiogram.utils.text_decorations import html_decoration

MESSAGE_LIMIT = 4096


def utf16_length(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


@dataclass(frozen=True, slots=True)
class MessageChunk:
    text: str
    entities: tuple[MessageEntity, ...]

    def as_html(self) -> str:
        return html_decoration.unparse(self.text, list(self.entities))


def split_entities(
    text: str,
    entities: Sequence[MessageEntity] = (),
    *,
    limit: int = MESSAGE_LIMIT,
) -> list[MessageChunk]:
    """Keep all text and formatting; never split a UTF-16 surrogate pair."""
    if limit < 2:
        raise ValueError("The text limit must be at least two UTF-16 units")
    chunks: list[MessageChunk] = []
    start = 0
    offset = 0
    while start < len(text):
        end, size = start, 0
        while end < len(text):
            width = 2 if ord(text[end]) > 0xFFFF else 1
            if size + width > limit:
                break
            size += width
            end += 1
        if end < len(text):
            newline = text.rfind("\n", start, end)
            if newline >= start and newline + 1 > start + (end - start) // 2:
                end = newline + 1
                size = utf16_length(text[start:end])
        clipped: list[MessageEntity] = []
        for entity in entities:
            left = max(offset, entity.offset)
            right = min(offset + size, entity.offset + entity.length)
            if left < right:
                clipped.append(entity.model_copy(update={"offset": left - offset, "length": right - left}))
        clipped.sort(key=lambda entity: (entity.offset, -entity.length))
        chunks.append(MessageChunk(text[start:end], tuple(clipped)))
        start = end
        offset += size
    return chunks


def split_formatted_text(content: Text, *, limit: int = MESSAGE_LIMIT) -> list[MessageChunk]:
    text, entities = content.render()
    return split_entities(text, entities, limit=limit)


class _HTMLToEntities(HTMLParser):
    """Parse the small, explicit HTML subset produced by catalog formatters."""

    _types = {
        "b": "bold",
        "strong": "bold",
        "i": "italic",
        "em": "italic",
        "u": "underline",
        "s": "strikethrough",
        "code": "code",
        "pre": "pre",
        "tg-spoiler": "spoiler",
        "a": "text_link",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.entities: list[MessageEntity] = []
        self.stack: list[tuple[str, int, str | None]] = []
        self.offset = 0

    def handle_data(self, data: str) -> None:
        self.parts.append(data)
        self.offset += utf16_length(data)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "br":
            self.handle_data("\n")
            return
        if tag not in self._types:
            raise ValueError(f"Unsupported Telegram HTML tag: {tag}")
        url = dict(attrs).get("href") if tag == "a" else None
        if tag == "a" and not url:
            raise ValueError("A Telegram link must have an href")
        self.stack.append((tag, self.offset, url))

    def handle_endtag(self, tag: str) -> None:
        if not self.stack or self.stack[-1][0] != tag:
            raise ValueError(f"Unbalanced Telegram HTML tag: {tag}")
        _, start, url = self.stack.pop()
        if self.offset > start:
            self.entities.append(
                MessageEntity(type=self._types[tag], offset=start, length=self.offset - start, url=url)
            )


def split_html(html: str, *, limit: int = MESSAGE_LIMIT) -> list[MessageChunk]:
    parser = _HTMLToEntities()
    parser.feed(html)
    parser.close()
    if parser.stack:
        raise ValueError("Unclosed Telegram HTML tags")
    return split_entities("".join(parser.parts), parser.entities, limit=limit)
