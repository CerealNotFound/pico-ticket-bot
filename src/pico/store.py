r"""Tiny SQLite store: de-duplicates incoming messages and tracks proposals.

Status flow: pending -> creating -> done
                     \-> rejected
A failed create goes back to pending so it can be retried (already-created tickets keep their URL).
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path

from pico.models import Ticket


@dataclass
class Proposal:
    id: str
    channel_id: str
    source_message_id: str  # the original message = thread root
    proposal_message_id: str  # the bot's message with the buttons
    source_text: str
    summary: str
    tickets: list[Ticket]
    status: str


class Store:
    def __init__(self, path: Path):
        self.db = sqlite3.connect(path, isolation_level=None)  # autocommit; single process
        self.db.row_factory = sqlite3.Row
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS seen (message_id TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS proposals (
                id TEXT PRIMARY KEY,
                channel_id TEXT NOT NULL,
                source_message_id TEXT NOT NULL,
                proposal_message_id TEXT NOT NULL,
                source_text TEXT NOT NULL,
                summary TEXT NOT NULL,
                tickets TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS proposals_source ON proposals (source_message_id);
            """
        )

    def first_time(self, message_id: str) -> bool:
        """True the first time a message id is seen (guards against duplicate events)."""
        cur = self.db.execute("INSERT OR IGNORE INTO seen (message_id) VALUES (?)", (message_id,))
        return cur.rowcount == 1

    def create(
        self, channel_id: str, source_message_id: str, proposal_message_id: str, source_text: str, summary: str,
        tickets: list[Ticket],
    ) -> Proposal:  # fmt: skip
        p = Proposal(
            uuid.uuid4().hex[:12], channel_id, source_message_id, proposal_message_id, source_text, summary,
            tickets, "pending",
        )  # fmt: skip
        self.db.execute(
            "INSERT INTO proposals (id, channel_id, source_message_id, proposal_message_id, source_text, summary,"
            " tickets, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (p.id, channel_id, source_message_id, proposal_message_id, source_text, summary, _dump(tickets), p.status),
        )
        return p

    def get(self, proposal_id: str) -> Proposal | None:
        row = self.db.execute("SELECT * FROM proposals WHERE id = ?", (proposal_id,)).fetchone()
        return _row(row) if row else None

    def by_thread(self, source_message_id: str) -> Proposal | None:
        row = self.db.execute(
            "SELECT * FROM proposals WHERE source_message_id = ? ORDER BY created_at DESC LIMIT 1",
            (source_message_id,),
        ).fetchone()
        return _row(row) if row else None

    def transition(self, proposal_id: str, from_status: str, to_status: str) -> bool:
        """Atomic compare-and-set; False if someone else already moved it (e.g. a double click)."""
        cur = self.db.execute(
            "UPDATE proposals SET status = ? WHERE id = ? AND status = ?", (to_status, proposal_id, from_status)
        )
        return cur.rowcount == 1

    def save_tickets(self, proposal_id: str, tickets: list[Ticket], summary: str | None = None) -> None:
        if summary is None:
            self.db.execute("UPDATE proposals SET tickets = ? WHERE id = ?", (_dump(tickets), proposal_id))
        else:
            self.db.execute(
                "UPDATE proposals SET tickets = ?, summary = ? WHERE id = ?", (_dump(tickets), summary, proposal_id)
            )


def _dump(tickets: list[Ticket]) -> str:
    return json.dumps([t.model_dump() for t in tickets])


def _row(row: sqlite3.Row) -> Proposal:
    return Proposal(
        row["id"], row["channel_id"], row["source_message_id"], row["proposal_message_id"], row["source_text"],
        row["summary"], [Ticket.model_validate(t) for t in json.loads(row["tickets"])], row["status"],
    )  # fmt: skip
