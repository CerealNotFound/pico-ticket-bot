"""Platform-neutral data shared by the LLM, chat and tracker layers."""

from __future__ import annotations

from dataclasses import dataclass, field

from pydantic import BaseModel


class Ticket(BaseModel):
    title: str
    description: str
    acceptance_criteria: list[str] = []
    priority: str | None = None
    labels: list[str] = []
    assignee: str | None = None
    # Filled in once the ticket exists in the tracker.
    url: str | None = None


class Draft(BaseModel):
    summary: str
    tickets: list[Ticket]


@dataclass
class BoardSchema:
    """What the tracker accepts, so the LLM only picks valid values."""

    priorities: list[str] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)
    assignees: list[str] = field(default_factory=list)
    allow_new_labels: bool = False


# --- Chat messages, rendered by each chat adapter in its own format ---------


@dataclass
class Span:
    text: str
    bold: bool = False
    italic: bool = False
    url: str | None = None


Line = list[Span]


@dataclass
class Button:
    label: str
    action: str
    value: str
    style: str = "primary"  # primary | secondary | warning | danger


@dataclass
class OutMessage:
    lines: list[Line]
    buttons: list[Button] = field(default_factory=list)

    @classmethod
    def plain(cls, text: str) -> OutMessage:
        return cls(lines=[[Span(line)] for line in text.split("\n")])

    def to_text(self) -> str:
        out = []
        for line in self.lines:
            out.append("".join(f"{s.text} ({s.url})" if s.url and s.url != s.text else s.text for s in line))
        return "\n".join(out)


@dataclass
class IncomingMessage:
    id: str
    channel_id: str
    user_id: str
    text: str
    thread_root_id: str | None = None


@dataclass
class ButtonClick:
    user_id: str
    channel_id: str
    message_id: str
    action: str
    value: str
