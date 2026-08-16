GEAR_SLOT_DEFINITIONS = (
    ("шлем", "🪖", "Шлем"),
    ("плечи", "🪹", "Плечи"),
    ("тело", "🦺", "Тело"),
    ("плащ", "🧣", "Плащ"),
    ("пояс", "⛓", "Пояс"),
    ("штаны", "🩳", "Штаны"),
    ("ботинки", "🥾", "Ботинки"),
    ("перчатки", "🧤", "Перчатки"),
    ("кольцо", "💍", "Кольцо"),
    ("амул", "📿", "Амулет"),
    ("серьга", "🧏‍♀️", "Серьга"),
    ("основная рука", "🗡", "Основная рука"),
    ("вторая рука", "🛡", "Вторая рука"),
)

GEAR_SLOTS = tuple(key for key, _, _ in GEAR_SLOT_DEFINITIONS)
GEAR_SLOT_LABELS = {
    key: f"{icon} {name}" for key, icon, name in GEAR_SLOT_DEFINITIONS
}
GEAR_SLOT_ICONS = {key: icon for key, icon, _ in GEAR_SLOT_DEFINITIONS}

RARITY_DEFINITIONS = (
    ("common", "⚪", "Обычное"),
    ("rare", "🟢", "Редкое"),
    ("epic", "🔵", "Сверхредкое"),
    ("legendary", "🟣", "Эпическое"),
)
RARITY_KEYS = tuple(key for key, _, _ in RARITY_DEFINITIONS)
RARITY_EMOJIS = {key: emoji for key, emoji, _ in RARITY_DEFINITIONS}
RARITY_NAMES = {key: name for key, _, name in RARITY_DEFINITIONS}
RESOURCE_TYPE_KEYS = (
    "craft",
    "consumable",
    "scroll_recipe",
    "currency",
    "alchemy",
)
GEAR_CLASS_ORDER = ("Аколит", "Бастион", "Маг", "Охотник", "Тень")
