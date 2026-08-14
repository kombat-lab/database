import html

def clean_username(username: str) -> str:
    """Убирает символ @ в начале, если есть."""
    return username.lstrip('@') if username else ''

def escape_html(text: object) -> str:
    """Экранирует HTML-спецсимволы."""
    return html.escape(str(text or ""), quote=True)

def is_valid_emoji(s: str) -> bool:
    """
    Проверяет, что строка не пустая и содержит хотя бы один символ,
    который не является буквой или цифрой (простейшая проверка на эмодзи).
    """
    if not s or not s.strip():
        return False
    # Убираем вариант с одним символом, который является буквой/цифрой
    if len(s) == 1 and s.isalnum():
        return False
    return True
