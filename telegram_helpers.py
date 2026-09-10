"""Runtime-checked access to optional Telegram update fields."""

from aiogram import Bot
from aiogram.types import CallbackQuery, Message, TelegramObject, User


def get_callback_message(callback: CallbackQuery) -> Message:
    message = callback.message
    if not isinstance(message, Message):
        raise ValueError("The callback message is inaccessible")
    return message


def get_callback_data(callback: CallbackQuery) -> str:
    if callback.data is None:
        raise ValueError("The callback does not contain data")
    return callback.data


def get_bound_bot(event: TelegramObject) -> Bot:
    bot = event.bot
    if bot is None:
        raise RuntimeError("The Telegram event is not bound to a bot")
    return bot


def get_message_user(message: Message) -> User:
    user = message.from_user
    if user is None:
        raise ValueError("The message has no user sender")
    return user


def get_message_text(message: Message) -> str:
    if message.text is None:
        raise ValueError("A text message is required")
    return message.text
