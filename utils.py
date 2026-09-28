import html

import emoji


RICH_TABLE_OPEN = "<table bordered striped compact>"


def clean_username(username: str) -> str:
    """Убирает символ @ в начале, если есть."""
    return username.lstrip("@") if username else ""


def escape_html(text: object) -> str:
    """Экранирует HTML-спецсимволы."""
    return html.escape("" if text is None else str(text), quote=True)


def is_valid_emoji(s: str) -> bool:
    """Разрешает до восьми Unicode emoji, включая составные последовательности."""
    if not s or len(s) > 64:
        return False
    matches = emoji.emoji_list(s)
    if not 1 <= len(matches) <= 8:
        return False
    end = 0
    for match in matches:
        if match["match_start"] != end:
            return False
        end = match["match_end"]
    return end == len(s)
