import html

def clean_username(username: str) -> str:
    """Убирает символ @ в начале, если есть."""
    return username.lstrip('@') if username else ''

def escape_html(text: str) -> str:
    """Экранирует HTML-спецсимволы."""
    return html.escape(text)

def is_valid_emoji(s: str) -> bool:
    """
    Проверяет, что строка не пустая (разрешает любые символы, включая составные эмодзи).
    """
    return bool(s and s.strip())
