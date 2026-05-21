import html
import re

def clean_username(username: str) -> str:
    return username.lstrip('@') if username else ''

def escape_html(text: str) -> str:
    return html.escape(text)

def is_valid_emoji(s: str) -> bool:
    """Проверяет, что строка состоит из 1-2 символов, не являющихся буквами, цифрами или пробелами."""
    if not s or not s.strip():
        return False
    s = s.strip()
    # Разрешаем только 1-2 символа, которые НЕ являются word characters (\w) или whitespace (\s)
    return bool(re.fullmatch(r'[^\w\s]{1,2}', s))
