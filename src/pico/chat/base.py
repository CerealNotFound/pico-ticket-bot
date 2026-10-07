"""Interface every chat platform implements (Pumble today; Slack etc. later)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol

from pico.models import ButtonClick, IncomingMessage, OutMessage

OnMessage = Callable[[IncomingMessage], Awaitable[None]]
OnClick = Callable[[ButtonClick], Awaitable[None]]


class ChatProvider(Protocol):
    name: str
    channel_id: str  # the channel the bot watches, resolved by setup()

    async def setup(self) -> None:
        """Validate credentials and resolve the watched channel. Raise with a helpful message on failure."""

    async def run(self, on_message: OnMessage, on_click: OnClick) -> None:
        """Receive events forever (reconnecting as needed). Must not deliver the bot's own messages,
        and must acknowledge button clicks immediately, before calling on_click."""

    async def post(self, message: OutMessage, thread_root_id: str | None = None) -> str:
        """Post to the watched channel (optionally as a thread reply). Returns the new message id."""

    async def edit(self, message_id: str, message: OutMessage) -> None: ...

    async def doctor(self) -> list[str]:
        """Human-readable diagnostic lines for `pico doctor`."""
