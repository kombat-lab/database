# Kombat Database Bot

Telegram-справочник по мобам, ресурсам, снаряжению, картам и рецептам. Бот работает на `aiogram 3`, хранит данные в SQLite и собирает встроенную аналитику.

## Требования

- Python 3.10–3.13;
- токен Telegram-бота;
- постоянный диск для SQLite в production.

## Запуск

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt

$env:BOT_TOKEN = "<telegram-token>"
$env:ADMIN_ID = "123456789,987654321"
$env:DATABASE_PATH = "game.db"
python bot.py
```

При первом запуске актуальная схема и все индексы создаются автоматически. Legacy-миграций в runtime нет: перенос выполняется отдельной проверяемой командой.

## Backup и миграция данных

Остановите бота, затем выполните:

```powershell
python scripts/migrate_database.py "C:\path\to\game.db" --backup-dir backups --keep 10 --yes
```

Команда:

1. отказывается работать, если база не найдена или занята;
2. создаёт SQLite-backup с timestamp, SHA-256 и JSON-манифестом;
3. проверяет backup через `PRAGMA integrity_check`;
4. создаёт новую базу с текущей схемой и переносит в неё все строки;
5. проверяет строки, внешние ключи и целостность;
6. атомарно заменяет рабочий файл и хранит последние 10 backup-копий.

Для восстановления из существующей копии используйте:

```powershell
python scripts/migrate_database.py "C:\path\to\game.db" --source-backup "C:\path\to\backup.db" --yes
```

Каталог `backups/` и SQLite-файлы исключены из Git, так как могут содержать production-данные.

## Переменные окружения

| Имя | Обязательна | Описание |
| --- | --- | --- |
| `BOT_TOKEN` | да | Токен от BotFather |
| `ADMIN_ID` | нет | Telegram ID администраторов через запятую |
| `DATABASE_PATH` | нет | Путь к SQLite, по умолчанию `game.db` |

Файл `.env.example` служит шаблоном. Проект не читает `.env` автоматически: передавайте переменные через shell или панель хостинга.

## Тесты

```powershell
python -m unittest discover -s tests -v
```

Тесты проверяют создание схемы, перенос данных и backup, транзакции, конкурентные вставки, каскадную очистку и разбор callback/deep-link данных.
