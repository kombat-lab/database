"""Validated contracts for durable gear editing and atomic catalog saves."""

from typing import Literal, NotRequired, TypedDict

from utils import is_valid_emoji

from game_constants import GEAR_CLASS_ORDER, GEAR_CLASS_SET, GEAR_SLOTS, RARITY_KEYS

MAX_NAME_LENGTH = 128
MAX_RESOURCE_NAME_LENGTH = 160
MAX_NOTE_LENGTH = 2000
MAX_CRAFT_LOCATION_LENGTH = 500
MAX_SQLITE_ID = 2**63 - 1


class DomainError(ValueError):
    """A catalog operation would violate game or editing invariants."""


class DraftConflictError(DomainError):
    """A stale button, context or concurrent edit cannot update this draft."""


class MaterialInput(TypedDict):
    quantity: int
    resource_id: NotRequired[int]
    name: NotRequired[str]
    emoji: NotRequired[str]


class LearningScrollInput(TypedDict, total=False):
    resource_id: int
    name: str
    emoji: str
    note: str


class GearDraftPayload(TypedDict, total=False):
    gear_id: int
    name: str
    rarity: str
    slot: str
    emoji: str
    level: int
    classes: str
    note: str
    craftable: bool
    quantity: int
    materials: list[MaterialInput]
    learning_scroll: LearningScrollInput | None
    gear_mob_ids: list[int]
    scroll_mob_ids: list[int]


class GearSaveResult(TypedDict):
    draft_id: str
    gear_id: int
    recipe_id: int | None
    scroll_resource_id: int | None


class GearDraft(TypedDict):
    draft_id: str
    owner_user_id: int
    chat_id: int
    message_id: int
    revision: int
    status: Literal['editing', 'saved', 'cancelled']
    payload: GearDraftPayload
    saved_result: GearSaveResult | None


class ResourceDependencies(TypedDict):
    ingredient_recipe_ids: list[int]
    learning_recipe_ids: list[int]
    result_recipe_ids: list[int]
    drop_mob_ids: list[int]


def positive_integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_SQLITE_ID:
        raise DomainError(f'{label}: нужно положительное целое число.')
    return value


def _text(value: object, label: str, maximum: int, *, required: bool = False) -> str:
    if not isinstance(value, str):
        raise DomainError(f'{label}: требуется текст.')
    if len(value) > maximum or (required and not value.strip()):
        raise DomainError(f'{label}: допустимо от {1 if required else 0} до {maximum} символов.')
    return value.strip() if required else value


def validate_craft_location(value: object) -> str:
    return _text(value, 'Место изготовления', MAX_CRAFT_LOCATION_LENGTH)


def _emoji(value: object, label: str) -> str:
    text = _text(value, label, 64)
    if text and not is_valid_emoji(text):
        raise DomainError(f'{label}: требуется Unicode эмодзи.')
    return text


def _ids(value: object, label: str) -> list[int]:
    if not isinstance(value, list) or len(value) > 1000:
        raise DomainError(f'{label}: требуется список идентификаторов.')
    result = [positive_integer(item, label) for item in value]
    if len(result) != len(set(result)):
        raise DomainError(f'{label}: повторяющиеся идентификаторы.')
    return result


def validate_draft_payload(value: object, *, complete: bool = False) -> GearDraftPayload:
    """Decode untrusted persisted JSON; partial drafts may omit unfinished fields."""
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise DomainError('Некорректный черновик.')
    allowed = {'gear_id', 'name', 'rarity', 'slot', 'emoji', 'level', 'classes', 'note',
               'craftable', 'quantity', 'materials', 'learning_scroll', 'gear_mob_ids', 'scroll_mob_ids'}
    if set(value) - allowed:
        raise DomainError('Неизвестные поля черновика.')
    result: GearDraftPayload = {}
    if 'gear_id' in value:
        result['gear_id'] = positive_integer(value['gear_id'], 'Снаряжение')
    if 'name' in value:
        result['name'] = _text(value['name'], 'Название', MAX_NAME_LENGTH, required=True)
    if 'rarity' in value:
        rarity = _text(value['rarity'], 'Редкость', 32, required=True)
        if rarity not in RARITY_KEYS:
            raise DomainError('Неизвестная редкость.')
        result['rarity'] = rarity
    if 'slot' in value:
        slot = _text(value['slot'], 'Слот', 64, required=True)
        if slot not in GEAR_SLOTS:
            raise DomainError('Неизвестный слот.')
        result['slot'] = slot
    if 'emoji' in value:
        result['emoji'] = _emoji(value['emoji'], 'Эмодзи')
    if 'level' in value:
        result['level'] = positive_integer(value['level'], 'Уровень')
    if 'classes' in value:
        raw_classes = _text(value['classes'], 'Классы', 128)
        classes = [part.strip() for part in raw_classes.split(',') if part.strip()]
        if any(item not in GEAR_CLASS_SET for item in classes):
            raise DomainError('Неизвестный класс снаряжения.')
        result['classes'] = ', '.join(item for item in GEAR_CLASS_ORDER if item in classes)
    if 'note' in value:
        result['note'] = _text(value['note'], 'Примечание', MAX_NOTE_LENGTH)
    if 'craftable' in value:
        if not isinstance(value['craftable'], bool):
            raise DomainError('Для признака изготовления требуется да/нет.')
        result['craftable'] = value['craftable']
    if 'quantity' in value:
        result['quantity'] = positive_integer(value['quantity'], 'Количество результата')
    if 'materials' in value:
        raw_materials = value['materials']
        if not isinstance(raw_materials, list) or len(raw_materials) > 200:
            raise DomainError('Некорректный список материалов.')
        materials: list[MaterialInput] = []
        for item in raw_materials:
            if (not isinstance(item, dict) or 'quantity' not in item
                    or set(item) - {'resource_id', 'quantity', 'name', 'emoji'}
                    or ('resource_id' in item) == ('name' in item)):
                raise DomainError('Укажите существующий resource_id или название нового материала и quantity.')
            material = MaterialInput(quantity=positive_integer(item['quantity'], 'Количество'))
            if 'resource_id' in item:
                material['resource_id'] = positive_integer(item['resource_id'], 'Ресурс')
            else:
                material['name'] = _text(item['name'], 'Название материала', MAX_RESOURCE_NAME_LENGTH, required=True)
            if 'emoji' in item:
                material['emoji'] = _emoji(item['emoji'], 'Эмодзи материала')
            materials.append(material)
        keys = [(str(item.get('resource_id')) if 'resource_id' in item
                 else 'name:' + item.get('name', '').casefold()) for item in materials]
        if len(set(keys)) != len(keys):
            raise DomainError('Материал повторяется: измените его количество.')
        result['materials'] = materials
    if 'learning_scroll' in value:
        raw_scroll = value['learning_scroll']
        if raw_scroll is None:
            result['learning_scroll'] = None
        else:
            if not isinstance(raw_scroll, dict) or set(raw_scroll) - {'resource_id', 'name', 'emoji', 'note'}:
                raise DomainError('Некорректное описание изучаемого свитка.')
            scroll: LearningScrollInput = {}
            if 'resource_id' in raw_scroll:
                scroll['resource_id'] = positive_integer(raw_scroll['resource_id'], 'Свиток')
            if 'name' in raw_scroll:
                scroll['name'] = _text(raw_scroll['name'], 'Название свитка', MAX_RESOURCE_NAME_LENGTH, required=True)
            if 'emoji' in raw_scroll:
                scroll['emoji'] = _emoji(raw_scroll['emoji'], 'Эмодзи свитка')
            if 'note' in raw_scroll:
                scroll['note'] = _text(raw_scroll['note'], 'Примечание свитка', MAX_NOTE_LENGTH)
            result['learning_scroll'] = scroll
    if 'gear_mob_ids' in value:
        result['gear_mob_ids'] = _ids(value['gear_mob_ids'], 'Источники снаряжения')
    if 'scroll_mob_ids' in value:
        result['scroll_mob_ids'] = _ids(value['scroll_mob_ids'], 'Источники свитка')
    if complete:
        if not all(key in result for key in ('name', 'rarity', 'slot')):
            raise DomainError('Заполните название, редкость и слот.')
        result.setdefault('emoji', '')
        result.setdefault('level', 1)
        result.setdefault('classes', '')
        result.setdefault('note', '')
        result.setdefault('craftable', False)
        result.setdefault('quantity', 1)
        result.setdefault('materials', [])
        result.setdefault('learning_scroll', None)
        result.setdefault('gear_mob_ids', [])
        result.setdefault('scroll_mob_ids', [])
        if result['craftable'] and not result['materials']:
            raise DomainError('Добавьте хотя бы один расходуемый материал.')
        if not result['craftable'] and (result['materials'] or result['learning_scroll'] is not None):
            raise DomainError('Материалы и изучение доступны только для изготовления.')
        if result['learning_scroll'] is None and result['scroll_mob_ids']:
            raise DomainError('Сначала выберите изучаемый свиток.')
    return result
