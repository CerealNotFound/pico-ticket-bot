"""All settings come from environment variables (loaded from .env)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]


def _list(name: str) -> list[str]:
    return [v.strip() for v in os.getenv(name, "").split(",") if v.strip()]


@dataclass
class Settings:
    # Pumble
    pumble_app_id: str = ""
    pumble_app_key: str = ""
    pumble_bot_token: str = ""
    pumble_channel: str = ""  # channel name or id
    pumble_approver_ids: list[str] = field(default_factory=list)

    # Notion
    notion_token: str = ""
    notion_database: str = ""  # board URL, database id or data source id
    notion_sop_page: str = ""  # optional page URL or id
    notion_default_status: str = ""
    notion_allow_new_labels: bool = False
    notion_props: dict[str, str] = field(default_factory=dict)

    # LLM
    llm_backend: str = "claude-cli"  # claude-cli | anthropic
    llm_model: str = ""

    sop_file: Path = ROOT / "sop.md"
    db_path: Path = ROOT / "pico.db"

    @classmethod
    def load(cls) -> Settings:
        load_dotenv(ROOT / ".env")
        e = os.getenv
        return cls(
            pumble_app_id=e("PUMBLE_APP_ID", ""),
            pumble_app_key=e("PUMBLE_APP_KEY", ""),
            pumble_bot_token=e("PUMBLE_BOT_TOKEN", ""),
            pumble_channel=e("PUMBLE_CHANNEL", "tickets").lstrip("#"),
            pumble_approver_ids=_list("PUMBLE_APPROVER_IDS"),
            notion_token=e("NOTION_TOKEN", ""),
            notion_database=e("NOTION_DATABASE", ""),
            notion_sop_page=e("NOTION_SOP_PAGE", ""),
            notion_default_status=e("NOTION_DEFAULT_STATUS", ""),
            notion_allow_new_labels=e("NOTION_ALLOW_NEW_LABELS", "").lower() in ("1", "true", "yes"),
            notion_props={
                k: e(f"NOTION_PROP_{k.upper()}", "")
                for k in ("priority", "labels", "status", "assignee")
                if e(f"NOTION_PROP_{k.upper()}")
            },
            llm_backend=e("LLM_BACKEND", "claude-cli"),
            llm_model=e("LLM_MODEL", ""),
        )
