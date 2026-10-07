"""Notion tracker against a mocked Notion API: id parsing, schema detection, page payload.

Run: uv run python tests/test_notion.py
"""

import asyncio
import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pico.models import Ticket
from pico.trackers.notion import NotionTracker, parse_notion_id

DB = "1f2e3d4c5b6a47988776655443322110"
DS = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

SCHEMA = {
    "object": "data_source",
    "id": DS,
    "title": [{"plain_text": "Engineering Board"}],
    "url": "https://www.notion.so/board",
    "properties": {
        "Task name": {"type": "title", "title": {}},
        "Status": {"type": "status", "status": {"options": [{"name": "Backlog"}, {"name": "In progress"}]}},
        "Priority": {"type": "select", "select": {"options": [{"name": "P0"}, {"name": "P1"}, {"name": "P2"}]}},
        "Tags": {"type": "multi_select", "multi_select": {"options": [{"name": "frontend"}, {"name": "backend"}]}},
        "Assignee": {"type": "people", "people": {}},
        "Due": {"type": "date", "date": {}},
    },
}

created = []


def handler(req: httpx.Request) -> httpx.Response:
    path = req.url.path
    if path == f"/v1/databases/{parse_notion_id(DB)}":
        return httpx.Response(
            200,
            json={
                "object": "database",
                "url": "https://www.notion.so/db",
                "data_sources": [{"id": DS, "name": "Board"}],
            },
        )
    if path == f"/v1/data_sources/{DS}":
        return httpx.Response(200, json=SCHEMA)
    if path == "/v1/users":
        return httpx.Response(
            200,
            json={
                "results": [
                    {"type": "person", "name": "Priya", "id": "u-1"},
                    {"type": "bot", "name": "Pico", "id": "b"},
                ],
                "has_more": False,
            },
        )
    if path == "/v1/pages" and req.method == "POST":
        created.append(json.loads(req.content))
        return httpx.Response(200, json={"object": "page", "id": "p1", "url": "https://www.notion.so/Fix-login-p1"})
    return httpx.Response(404, json={"object": "error", "status": 404, "code": "object_not_found", "message": path})


async def main():
    assert parse_notion_id(f"https://www.notion.so/acme/{DB}?v=0123456789abcdef0123456789abcdef") == parse_notion_id(DB)
    assert parse_notion_id(f"https://www.notion.so/acme/My-Board-{DB}") == "1f2e3d4c-5b6a-4798-8776-655443322110"

    t = NotionTracker("tok", f"https://www.notion.so/acme/{DB}?v=1234", default_status="Backlog")
    t.client.client = httpx.AsyncClient(base_url="https://api.notion.com/v1/", transport=httpx.MockTransport(handler))
    await t.setup()
    assert t.data_source_id == DS
    assert t.title_prop == "Task name"
    assert t.props == {"priority": "Priority", "labels": "Tags", "status": "Status", "assignee": "Assignee"}, t.props
    s = t.schema()
    assert s.priorities == ["P0", "P1", "P2"] and s.labels == ["frontend", "backend"] and s.assignees == ["Priya"]

    url = await t.create(
        Ticket(
            title="Fix login",
            description="Para one.\n\nPara two " + "x" * 2500,
            acceptance_criteria=["works on Safari"],
            priority="P1",
            labels=["frontend", "made-up"],
            assignee="Priya",
        )
    )
    assert url.startswith("https://www.notion.so/")
    body = created[0]
    assert body["parent"] == {"type": "data_source_id", "data_source_id": DS}
    p = body["properties"]
    assert p["Task name"]["title"][0]["text"]["content"] == "Fix login"
    assert p["Priority"] == {"select": {"name": "P1"}}
    assert p["Tags"] == {"multi_select": [{"name": "frontend"}]}, "unknown labels must be dropped"
    assert p["Status"] == {"status": {"name": "Backlog"}}
    assert p["Assignee"] == {"people": [{"id": "u-1"}]}
    kinds = [b["type"] for b in body["children"]]
    assert kinds == ["paragraph", "paragraph", "heading_3", "to_do"], kinds
    assert all(len(r["text"]["content"]) <= 2000 for b in body["children"] for r in b[b["type"]]["rich_text"])
    print("\n".join(await t.doctor()))
    print("ALL NOTION TESTS PASSED")


asyncio.run(main())
