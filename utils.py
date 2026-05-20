import html

def clean_username(username: str) -> str:
    """Убирает символ @ в начале, если есть."""
    return username.lstrip('@') if username else ''

def escape_html(text: str) -> str:
    """Экранирует HTML-спецсимволы."""
    return html.escape(text)

def is_valid_emoji(s: str) -> bool:
    """Проверяет, что строка содержит 1 или 2 символа, и ни один из них не является буквой или цифрой."""
    if not s or len(s) not in (1, 2):
        return False
    return all(not ch.isalnum() for ch in s)
