from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import time
from dataclasses import dataclass, field
from typing import Any

from telegram import (
    CallbackQuery,
    Chat,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Location,
    Message,
    MessageEntity,
    PhotoSize,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
    User,
)
from telegram.constants import ChatType, MessageEntityType
from telegram.ext import ExtBot

# Support query.bot access in handlers
if not hasattr(CallbackQuery, "bot"):
    CallbackQuery.bot = property(lambda self: self.get_bot())


@dataclass
class ButtonSnapshot:
    text: str
    callback_data: str | None = None
    url: str | None = None


@dataclass
class ChatMessageRecord:
    id: int
    chat_id: int
    user_id: int
    text: str
    parse_mode: str | None
    inline_keyboard: list[list[ButtonSnapshot]] = field(default_factory=list)
    reply_keyboard: list[list[str]] = field(default_factory=list)
    photo_file_id: str | None = None
    is_bot: bool = False
    is_deleted: bool = False
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "chat_id": self.chat_id,
            "user_id": self.user_id,
            "text": self.text,
            "parse_mode": self.parse_mode,
            "inline_keyboard": [
                [{"text": b.text, "callback_data": b.callback_data, "url": b.url} for b in row]
                for row in self.inline_keyboard
            ],
            "reply_keyboard": self.reply_keyboard,
            "photo_file_id": self.photo_file_id,
            "is_bot": self.is_bot,
            "is_deleted": self.is_deleted,
            "timestamp": self.timestamp,
        }


@dataclass
class ScreenState:
    chat_id: int
    user_id: int
    active_message_id: int | None = None
    text: str = ""
    parse_mode: str | None = None
    inline_keyboard: list[list[ButtonSnapshot]] = field(default_factory=list)
    reply_keyboard: list[list[str]] = field(default_factory=list)
    photo_file_id: str | None = None
    last_alert: str | None = None
    last_action_timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "chat_id": self.chat_id,
            "user_id": self.user_id,
            "active_message_id": self.active_message_id,
            "text": self.text,
            "parse_mode": self.parse_mode,
            "inline_keyboard": [
                [{"text": b.text, "callback_data": b.callback_data, "url": b.url} for b in row]
                for row in self.inline_keyboard
            ],
            "reply_keyboard": self.reply_keyboard,
            "photo_file_id": self.photo_file_id,
            "last_alert": self.last_alert,
            "last_action_timestamp": self.last_action_timestamp,
        }


def _extract_inline_buttons(reply_markup: Any) -> list[list[ButtonSnapshot]]:
    if not isinstance(reply_markup, InlineKeyboardMarkup):
        return []
    result: list[list[ButtonSnapshot]] = []
    for row in reply_markup.inline_keyboard:
        btn_row: list[ButtonSnapshot] = []
        for btn in row:
            if isinstance(btn, InlineKeyboardButton):
                btn_row.append(ButtonSnapshot(
                    text=btn.text,
                    callback_data=btn.callback_data,
                    url=btn.url,
                ))
        result.append(btn_row)
    return result


def _extract_reply_buttons(reply_markup: Any) -> list[list[str]]:
    if isinstance(reply_markup, ReplyKeyboardRemove):
        return []
    if not isinstance(reply_markup, ReplyKeyboardMarkup):
        return []
    result: list[list[str]] = []
    for row in reply_markup.keyboard:
        btn_row: list[str] = []
        for btn in row:
            if isinstance(btn, KeyboardButton):
                btn_row.append(btn.text)
            elif isinstance(btn, str):
                btn_row.append(btn)
        result.append(btn_row)
    return result


class ScreenStateTracker:
    """Manages chat messages and current screen state per user/chat session."""

    def __init__(self) -> None:
        self.messages: dict[int, list[ChatMessageRecord]] = {}  # chat_id -> list of records
        self.message_lookup: dict[tuple[int, int], ChatMessageRecord] = {}  # (chat_id, msg_id) -> record
        self.screens: dict[int, ScreenState] = {}  # chat_id -> ScreenState
        self.next_message_id = 1000
        self._lock = asyncio.Lock()

    def _get_next_id(self) -> int:
        self.next_message_id += 1
        return self.next_message_id

    async def record_user_message(
        self,
        chat_id: int,
        user_id: int,
        text: str,
        location: tuple[float, float] | None = None,
    ) -> ChatMessageRecord:
        async with self._lock:
            msg_id = self._get_next_id()
            display_text = f"📍 ({location[0]}, {location[1]})" if location else text
            record = ChatMessageRecord(
                id=msg_id,
                chat_id=chat_id,
                user_id=user_id,
                text=display_text,
                parse_mode=None,
                is_bot=False,
            )
            self.messages.setdefault(chat_id, []).append(record)
            self.message_lookup[(chat_id, msg_id)] = record
            return record

    async def record_bot_message(
        self,
        chat_id: int,
        text: str,
        reply_markup: Any = None,
        parse_mode: str | None = None,
        photo_file_id: str | None = None,
    ) -> ChatMessageRecord:
        async with self._lock:
            msg_id = self._get_next_id()
            inline_kb = _extract_inline_buttons(reply_markup)
            reply_kb = _extract_reply_buttons(reply_markup)

            record = ChatMessageRecord(
                id=msg_id,
                chat_id=chat_id,
                user_id=0,
                text=text,
                parse_mode=parse_mode,
                inline_keyboard=inline_kb,
                reply_keyboard=reply_kb,
                photo_file_id=photo_file_id,
                is_bot=True,
            )
            self.messages.setdefault(chat_id, []).append(record)
            self.message_lookup[(chat_id, msg_id)] = record

            # Update active screen state
            screen = self.screens.setdefault(chat_id, ScreenState(chat_id=chat_id, user_id=chat_id))
            screen.active_message_id = msg_id
            screen.text = text
            screen.parse_mode = parse_mode
            screen.inline_keyboard = inline_kb
            if isinstance(reply_markup, ReplyKeyboardMarkup):
                screen.reply_keyboard = reply_kb
            elif isinstance(reply_markup, ReplyKeyboardRemove):
                screen.reply_keyboard = []
            screen.photo_file_id = photo_file_id
            screen.last_action_timestamp = time.time()

            return record

    async def record_message_edit(
        self,
        chat_id: int,
        message_id: int,
        text: str | None = None,
        reply_markup: Any = None,
        parse_mode: str | None = None,
    ) -> ChatMessageRecord | None:
        async with self._lock:
            record = self.message_lookup.get((chat_id, message_id))
            if not record:
                # If message was not found, create one
                return await self.record_bot_message(chat_id, text or "", reply_markup, parse_mode)

            if text is not None:
                record.text = text
            if parse_mode is not None:
                record.parse_mode = parse_mode
            if reply_markup is not None:
                record.inline_keyboard = _extract_inline_buttons(reply_markup)

            # Update active screen state
            screen = self.screens.setdefault(chat_id, ScreenState(chat_id=chat_id, user_id=chat_id))
            screen.active_message_id = message_id
            if text is not None:
                screen.text = text
            if parse_mode is not None:
                screen.parse_mode = parse_mode
            if reply_markup is not None:
                screen.inline_keyboard = record.inline_keyboard
            screen.last_action_timestamp = time.time()
            return record

    async def record_message_delete(self, chat_id: int, message_id: int) -> bool:
        async with self._lock:
            record = self.message_lookup.get((chat_id, message_id))
            if record:
                record.is_deleted = True
            return True

    async def record_alert(self, chat_id: int, alert_text: str) -> None:
        async with self._lock:
            screen = self.screens.setdefault(chat_id, ScreenState(chat_id=chat_id, user_id=chat_id))
            screen.last_alert = alert_text

    def get_screen(self, chat_id: int) -> ScreenState:
        return self.screens.get(chat_id, ScreenState(chat_id=chat_id, user_id=chat_id))

    def get_transcript(self, chat_id: int) -> list[ChatMessageRecord]:
        return self.messages.get(chat_id, [])

    def reset(self, chat_id: int | None = None) -> None:
        if chat_id is not None:
            self.messages.pop(chat_id, None)
            self.screens.pop(chat_id, None)
            keys_to_del = [k for k in self.message_lookup if k[0] == chat_id]
            for k in keys_to_del:
                self.message_lookup.pop(k, None)
        else:
            self.messages.clear()
            self.message_lookup.clear()
            self.screens.clear()


# Global tracker instance
tracker = ScreenStateTracker()


class MockTelegramBot(ExtBot):
    """Mock Telegram Bot adapter that intercepts all PTB API actions into the ScreenStateTracker."""

    def __init__(self, token: str = "123456:MOCK_TOKEN_FOR_AGENT_HARNESS") -> None:
        super().__init__(token=token)
        self._mock_user = User(
            id=99999999,
            first_name="Mock Logistics Bot",
            is_bot=True,
            username="MockLogisticsBot",
        )
        self._bot_user = self._mock_user
        self._bot = self._mock_user
        self._bot_initialized = True
        self._requests_initialized = True

    async def initialize(self) -> None:
        self._bot_user = self._mock_user
        self._bot = self._mock_user
        self._bot_initialized = True
        self._requests_initialized = True

    async def shutdown(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def get_me(self, *args: Any, **kwargs: Any) -> User:
        return self._mock_user

    async def send_message(
        self,
        chat_id: int,
        text: str,
        parse_mode: str | None = None,
        reply_markup: Any = None,
        **kwargs: Any,
    ) -> Message:
        record = await tracker.record_bot_message(
            chat_id=chat_id,
            text=text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
        )
        return _make_ptb_message(
            message_id=record.id,
            chat_id=chat_id,
            from_user=await self.get_me(),
            text=text,
            reply_markup=reply_markup,
            bot=self,
        )

    async def edit_message_text(
        self,
        text: str,
        chat_id: int | None = None,
        message_id: int | None = None,
        parse_mode: str | None = None,
        reply_markup: Any = None,
        **kwargs: Any,
    ) -> Message | bool:
        if chat_id is None:
            chat_id = kwargs.get("chat_id", 0)
        if message_id is None:
            message_id = kwargs.get("message_id", 0)

        await tracker.record_message_edit(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
        )
        return _make_ptb_message(
            message_id=message_id,
            chat_id=chat_id,
            from_user=await self.get_me(),
            text=text,
            reply_markup=reply_markup,
            bot=self,
        )

    async def edit_message_reply_markup(
        self,
        chat_id: int | None = None,
        message_id: int | None = None,
        reply_markup: Any = None,
        **kwargs: Any,
    ) -> Message | bool:
        if chat_id is None:
            chat_id = kwargs.get("chat_id", 0)
        if message_id is None:
            message_id = kwargs.get("message_id", 0)

        await tracker.record_message_edit(
            chat_id=chat_id,
            message_id=message_id,
            reply_markup=reply_markup,
        )
        return True

    async def delete_message(
        self,
        chat_id: int,
        message_id: int,
        **kwargs: Any,
    ) -> bool:
        return await tracker.record_message_delete(chat_id=chat_id, message_id=message_id)

    async def send_photo(
        self,
        chat_id: int,
        photo: Any,
        caption: str | None = None,
        parse_mode: str | None = None,
        reply_markup: Any = None,
        **kwargs: Any,
    ) -> Message:
        file_id = str(photo) if isinstance(photo, str) else "photo_mock_file_id"
        record = await tracker.record_bot_message(
            chat_id=chat_id,
            text=caption or "[Photo]",
            reply_markup=reply_markup,
            parse_mode=parse_mode,
            photo_file_id=file_id,
        )
        return _make_ptb_message(
            message_id=record.id,
            chat_id=chat_id,
            from_user=await self.get_me(),
            text=caption or "[Photo]",
            reply_markup=reply_markup,
            photo=[PhotoSize(file_id=file_id, file_unique_id=file_id, width=100, height=100)],
            bot=self,
        )

    async def answer_callback_query(
        self,
        callback_query_id: str,
        text: str | None = None,
        show_alert: bool | None = None,
        **kwargs: Any,
    ) -> bool:
        if text:
            # Record alert if present
            chat_id = int(callback_query_id.split("_")[0]) if "_" in callback_query_id else 0
            if chat_id:
                await tracker.record_alert(chat_id, text)
        return True


def _make_ptb_message(
    message_id: int,
    chat_id: int,
    from_user: User,
    text: str,
    reply_markup: Any = None,
    photo: list[PhotoSize] | None = None,
    location: Location | None = None,
    bot: Any = None,
) -> Message:
    now_utc = datetime.now(UTC)
    chat = Chat(id=chat_id, type=ChatType.PRIVATE, first_name=from_user.first_name, username=from_user.username)
    msg = Message(
        message_id=message_id,
        date=now_utc,
        chat=chat,
        from_user=from_user,
        text=text,
        reply_markup=reply_markup,
        photo=photo,
        location=location,
    )
    msg.set_bot(bot)
    return msg


def make_user_update(
    user_id: int,
    text: str | None = None,
    callback_data: str | None = None,
    latitude: float | None = None,
    longitude: float | None = None,
    username: str = "agent_tester",
    first_name: str = "Agent",
    bot: Any = None,
) -> tuple[Update, ChatMessageRecord]:
    now_utc = datetime.now(UTC)
    user = User(id=user_id, is_bot=False, first_name=first_name, username=username)
    chat = Chat(id=user_id, type=ChatType.PRIVATE, first_name=first_name, username=username)
    msg_id = tracker._get_next_id()

    loc = Location(latitude=latitude, longitude=longitude) if latitude is not None and longitude is not None else None

    if callback_data is not None:
        # Callback query update
        screen = tracker.get_screen(user_id)
        active_msg_id = screen.active_message_id or msg_id
        parent_msg = _make_ptb_message(
            message_id=active_msg_id,
            chat_id=user_id,
            from_user=User(id=99999999, is_bot=True, first_name="Mock Logistics Bot", username="MockLogisticsBot"),
            text=screen.text,
            bot=bot,
        )
        cq = CallbackQuery(
            id=f"{user_id}_{msg_id}",
            from_user=user,
            chat_instance=str(user_id),
            data=callback_data,
            message=parent_msg,
        )
        cq.set_bot(bot)
        update = Update(update_id=msg_id, callback_query=cq)
        record = ChatMessageRecord(
            id=msg_id,
            chat_id=user_id,
            user_id=user_id,
            text=f"[Clicked: {callback_data}]",
            parse_mode=None,
            is_bot=False,
        )
        tracker.messages.setdefault(user_id, []).append(record)
        return update, record

    entities = None
    if text and text.startswith("/"):
        cmd_word = text.split()[0]
        entities = [
            MessageEntity(
                type=MessageEntityType.BOT_COMMAND,
                offset=0,
                length=len(cmd_word),
            )
        ]

    # Message update
    msg = Message(
        message_id=msg_id,
        date=now_utc,
        chat=chat,
        from_user=user,
        text=text,
        location=loc,
        entities=entities,
    )
    msg.set_bot(bot)
    update = Update(update_id=msg_id, message=msg)

    display_text = f"📍 ({latitude}, {longitude})" if loc else (text or "")
    record = ChatMessageRecord(
        id=msg_id,
        chat_id=user_id,
        user_id=user_id,
        text=display_text,
        parse_mode=None,
        is_bot=False,
    )
    tracker.messages.setdefault(user_id, []).append(record)
    tracker.message_lookup[(user_id, msg_id)] = record
    return update, record
