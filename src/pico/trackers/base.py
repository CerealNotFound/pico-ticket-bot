"""Interface every ticket tracker implements (Notion today; Linear etc. later)."""

from __future__ import annotations

from typing import Protocol

from pico.models import BoardSchema, Ticket


class Tracker(Protocol):
    name: str
    board_url: str | None

    async def setup(self) -> None:
        """Validate credentials and load the board schema. Raise with a helpful message on failure."""

    def schema(self) -> BoardSchema: ...

    async def standards(self) -> str | None:
        """Team ticket standards / SOP text, if configured."""

    async def create(self, ticket: Ticket) -> str:
        """Create one ticket and return its URL."""

    async def doctor(self) -> list[str]:
        """Human-readable diagnostic lines for `pico doctor`."""
