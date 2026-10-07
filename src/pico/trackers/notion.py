"""Notion tracker: one page per ticket in a database (board).

Reads the board's schema at startup and only ever writes values that exist,
which is what keeps Notion from rejecting pages.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from notion_client import APIResponseError, AsyncClient

from pico.models import BoardSchema, Ticket

log = logging.getLogger(__name__)

NOTION_VERSION = "2025-09-03"
MAX_TEXT = 2000  # Notion's limit per rich_text object

# Property names we look for when no explicit NOTION_PROP_* override is set.
GUESSES = {
    "priority": ("priority", "prio", "severity", "importance"),
    "labels": ("labels", "label", "tags", "tag", "type", "area", "component", "category"),
    "status": ("status", "state", "stage"),
    "assignee": ("assignee", "assignees", "assigned to", "assigned", "owner", "owners"),
}
TYPES = {"priority": ("select",), "labels": ("multi_select",), "status": ("status",), "assignee": ("people",)}


def parse_notion_id(value: str) -> str:
    """Accepts a Notion URL, a 32-char id or a dashed UUID; returns the dashed UUID."""
    value = value.strip()
    path = value.split("?")[0].split("#")[0]
    matches = re.findall(r"[0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{12}", path)
    if not matches:
        raise ValueError(f"Can't find a Notion id in {value!r}")
    raw = matches[-1].replace("-", "").lower()
    return f"{raw[:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:]}"


def _text(content: str) -> list[dict]:
    return [{"type": "text", "text": {"content": content[i : i + MAX_TEXT]}} for i in range(0, len(content), MAX_TEXT)][
        :100
    ] or [{"type": "text", "text": {"content": ""}}]


def _block(kind: str, content: str, **extra) -> dict:
    return {"object": "block", "type": kind, kind: {"rich_text": _text(content), **extra}}


class NotionTracker:
    name = "Notion"

    def __init__(
        self,
        token: str,
        database: str,
        sop_page: str = "",
        sop_file: Path | None = None,
        default_status: str = "",
        allow_new_labels: bool = False,
        prop_overrides: dict[str, str] | None = None,
    ):
        self.client = AsyncClient(auth=token, notion_version=NOTION_VERSION)
        self.database = database
        self.sop_page = sop_page
        self.sop_file = sop_file
        self.default_status = default_status
        self.allow_new_labels = allow_new_labels
        self.overrides = prop_overrides or {}

        self.data_source_id = ""
        self.board_name = ""
        self.board_url: str | None = None
        self.title_prop = ""
        self.props: dict[str, str | None] = {}  # role -> property name
        self.options: dict[str, list[str]] = {}  # role -> allowed option names
        self.users: dict[str, str] = {}  # display name -> user id
        self.warnings: list[str] = []

    # --- setup ------------------------------------------------------------------

    async def setup(self) -> None:
        if not self.database:
            raise RuntimeError("NOTION_DATABASE is not set (paste your board's URL into .env).")
        board_id = parse_notion_id(self.database)
        try:
            db = await self.client.databases.retrieve(database_id=board_id)
            sources = db.get("data_sources") or []
            if not sources:
                raise RuntimeError("That Notion database has no data sources.")
            if len(sources) > 1:
                self.warnings.append(
                    f"Database has {len(sources)} data sources; using the first ({sources[0]['name']})."
                )
            self.data_source_id = sources[0]["id"]
            self.board_url = db.get("url")
        except APIResponseError as e:
            if e.status != 404:
                raise RuntimeError(_explain(e)) from e
            # Maybe they gave us a data source id directly.
            self.data_source_id = board_id
        try:
            ds = await self.client.data_sources.retrieve(data_source_id=self.data_source_id)
        except APIResponseError as e:
            raise RuntimeError(_explain(e)) from e

        self.board_name = "".join(t.get("plain_text", "") for t in ds.get("title", [])) or "(untitled)"
        self.board_url = self.board_url or ds.get("url")
        properties: dict[str, dict] = ds["properties"]
        self.title_prop = next(name for name, p in properties.items() if p["type"] == "title")

        for role, types in TYPES.items():
            name = self._pick(role, types, properties)
            self.props[role] = name
            if name and role != "assignee":
                kind = properties[name]["type"]
                self.options[role] = [o["name"] for o in properties[name][kind].get("options", [])]

        if self.default_status and self.default_status not in self.options.get("status", []):
            self.warnings.append(
                f"NOTION_DEFAULT_STATUS={self.default_status!r} is not a status on the board "
                f"(options: {', '.join(self.options.get('status', [])) or 'none'}); leaving status unset."
            )
            self.default_status = ""

        if self.props.get("assignee"):
            await self._load_users()

    def _pick(self, role: str, types: tuple[str, ...], properties: dict[str, dict]) -> str | None:
        override = self.overrides.get(role)
        if override:
            if override not in properties:
                raise RuntimeError(f"NOTION_PROP_{role.upper()}={override!r} is not a property on the board.")
            if properties[override]["type"] not in types:
                raise RuntimeError(
                    f"NOTION_PROP_{role.upper()}={override!r} is a {properties[override]['type']} property; "
                    f"expected {' or '.join(types)}."
                )
            return override
        candidates = [n for n, p in properties.items() if p["type"] in types]
        for guess in GUESSES[role]:
            for name in candidates:
                if name.strip().lower() == guess:
                    return name
        # Only one property of the right type -> safe to assume it's the one.
        return candidates[0] if len(candidates) == 1 else None

    async def _load_users(self) -> None:
        try:
            cursor = None
            while True:
                page = await self.client.users.list(start_cursor=cursor) if cursor else await self.client.users.list()
                for u in page["results"]:
                    if u.get("type") == "person" and u.get("name"):
                        self.users[u["name"]] = u["id"]
                if not page.get("has_more"):
                    break
                cursor = page["next_cursor"]
        except APIResponseError as e:
            self.warnings.append(
                "Can't list Notion users, so tickets won't be assigned. Enable 'Read user information' "
                f"on the Notion connection to fix this ({e.code})."
            )

    # --- Tracker interface ------------------------------------------------------------

    def schema(self) -> BoardSchema:
        return BoardSchema(
            priorities=self.options.get("priority", []),
            labels=self.options.get("labels", []) if self.props.get("labels") else [],
            assignees=sorted(self.users),
            allow_new_labels=self.allow_new_labels and bool(self.props.get("labels")),
        )

    async def standards(self) -> str | None:
        if self.sop_page:
            try:
                page = await self.client.pages.retrieve_markdown(page_id=parse_notion_id(self.sop_page))
                if page.get("markdown", "").strip():
                    return page["markdown"]
            except (APIResponseError, ValueError) as e:
                log.warning("Couldn't read the SOP page from Notion: %s", e)
        if self.sop_file and self.sop_file.exists():
            text = self.sop_file.read_text().strip()
            return text or None
        return None

    async def create(self, ticket: Ticket) -> str:
        props: dict = {self.title_prop: {"title": _text(ticket.title[:MAX_TEXT])}}

        if (name := self.props.get("priority")) and ticket.priority in self.options.get("priority", []):
            props[name] = {"select": {"name": ticket.priority}}

        if name := self.props.get("labels"):
            allowed = self.options.get("labels", [])
            labels = [x for x in ticket.labels if self.allow_new_labels or x in allowed]
            if labels:
                props[name] = {"multi_select": [{"name": x.replace(",", " ")[:100]} for x in labels]}

        if (name := self.props.get("status")) and self.default_status:
            props[name] = {"status": {"name": self.default_status}}

        if (name := self.props.get("assignee")) and ticket.assignee in self.users:
            props[name] = {"people": [{"id": self.users[ticket.assignee]}]}

        children = [_block("paragraph", p.strip()) for p in ticket.description.split("\n\n") if p.strip()]
        if ticket.acceptance_criteria:
            children.append(_block("heading_3", "Acceptance criteria"))
            children += [_block("to_do", c, checked=False) for c in ticket.acceptance_criteria]

        try:
            page = await self.client.pages.create(
                parent={"type": "data_source_id", "data_source_id": self.data_source_id},
                properties=props,
                children=children[:100],
            )
        except APIResponseError as e:
            raise RuntimeError(_explain(e)) from e
        return page["url"]

    async def doctor(self) -> list[str]:
        lines = [f"Board: {self.board_name}  ({self.board_url})", f"  title property     -> {self.title_prop}"]
        for role in TYPES:
            name = self.props.get(role)
            detail = ""
            if name and role in self.options:
                detail = f"  options: {', '.join(self.options[role]) or '(none)'}"
            if name and role == "assignee":
                detail = f"  {len(self.users)} Notion users found"
            lines.append(f"  {role:<18} -> {name or '(not found - skipped)'}{detail}")
        if self.props.get("status"):
            lines.append(f"  new tickets status -> {self.default_status or '(board default)'}")
        sop = await self.standards()
        source = "Notion page" if self.sop_page else str(self.sop_file.name if self.sop_file else "")
        loaded = f"loaded from {source} ({len(sop)} chars)" if sop else "none (using built-in defaults)"
        lines.append(f"Ticket standards: {loaded}")
        lines += [f"⚠️  {w}" for w in self.warnings]
        return lines


def _explain(e: APIResponseError) -> str:
    if e.status == 404:
        return (
            "Notion says the board/page doesn't exist. Usually this means it isn't shared with your connection: "
            "open the board in Notion → ••• → Connections → add your Pico connection."
        )
    if e.status == 401:
        return "Notion rejected NOTION_TOKEN (401). Copy the token again from your connection's Configuration tab."
    if e.status == 403:
        return f"Notion connection is missing a capability ({e.message}). Enable Read + Insert content."
    return f"Notion error {e.status} {e.code}: {e.message}"
