"""End-to-end test of the Pumble socket adapter + flow against a fake Pumble (websocket + REST).

Run: uv run python tests/test_flow.py
"""

import asyncio
import json
import sys
import tempfile
from pathlib import Path

import httpx
import websockets

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pico.chat.pumble import PumbleChat
from pico.flow import APPROVE, REJECT, Flow
from pico.models import BoardSchema, Draft, Ticket
from pico.store import Store

CHANNEL, BOT, USER = "C1", "BOT", "U1"


class FakePumbleREST:
    def __init__(self):
        self.calls: list[tuple[str, str, dict]] = []
        self.n = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        self.calls.append((request.method, request.url.path, body))
        if request.url.path == "/oauth2/me":
            return httpx.Response(200, json={"workspaceUserId": BOT, "workspaceUserName": "pico"})
        if request.url.path == "/v1/channels":
            return httpx.Response(200, json=[{"channel": {"id": CHANNEL, "name": "tickets", "channelType": "PUBLIC"}}])
        if request.method == "POST":
            self.n += 1
            return httpx.Response(200, json={"id": f"M{self.n}"})
        return httpx.Response(200, json={})

    def texts(self, method=None):
        return [(m, p, b.get("text", "")) for m, p, b in self.calls if b and (method is None or m == method)]


class FakeTracker:
    name = "Notion"
    board_url = "https://notion.so/board"

    def __init__(self):
        self.created: list[str] = []
        self.fail_once = set()

    def schema(self):
        return BoardSchema(priorities=["High", "Low"])

    async def standards(self):
        return "Titles must start with a verb."

    async def create(self, t: Ticket) -> str:
        if t.title in self.fail_once:
            self.fail_once.discard(t.title)
            raise RuntimeError("boom")
        self.created.append(t.title)
        return f"https://notion.so/{t.title.replace(' ', '-')}"


class FakeWriter:
    name = "fake"

    def __init__(self):
        self.calls = []

    async def draft(self, text, board, standards=None, previous=None, feedback=None):
        self.calls.append((text, standards, feedback))
        if "nothing" in text:
            return Draft(summary="chit-chat", tickets=[])
        if feedback:
            return Draft(summary="revised", tickets=[Ticket(title="Merged ticket", description="d", priority="High")])
        return Draft(
            summary="two things",
            tickets=[
                Ticket(title="Fix login", description="Safari", acceptance_criteria=["works"], priority="High"),
                Ticket(title="Add CSV export", description="reports"),
            ],
        )


def event(mid, text, author=USER, thread=None, channel=CHANNEL):
    body = {"mId": mid, "cId": channel, "aId": author, "tx": text, "trId": thread or ""}
    return json.dumps(
        {"payload": {"messageType": "PUMBLE_EVENT", "eventType": "NEW_MESSAGE", "body": json.dumps(body)}}
    )


def click(cid, action, value, msg="M1"):
    payload = {
        "messageType": "BLOCK_INTERACTION", "sourceType": "MESSAGE", "userId": USER, "channelId": CHANNEL,
        "sourceId": msg, "onAction": action, "payload": json.dumps({"value": value}), "actionType": "button",
    }  # fmt: skip
    return json.dumps({"payload": payload, "correlation_id": cid})


async def main():
    to_send: asyncio.Queue = asyncio.Queue()
    received: list[str] = []

    async def server(ws):
        async def reader():
            async for m in ws:
                received.append(m)
                if m == "ping":
                    await ws.send("pong")

        r = asyncio.create_task(reader())
        while True:
            frame = await to_send.get()
            await ws.send(frame)
        r.cancel()

    srv = await websockets.serve(server, "127.0.0.1", 0)
    port = srv.sockets[0].getsockname()[1]

    rest = FakePumbleREST()
    chat = PumbleChat("app", "key", "tok", "tickets")
    chat.http = httpx.AsyncClient(base_url="https://api-ga.pumble.com", transport=httpx.MockTransport(rest.handler))

    async def socket_url():
        return f"ws://127.0.0.1:{port}"

    chat._socket_url = socket_url
    await chat.setup()
    assert chat.channel_id == CHANNEL and chat.bot_user_id == BOT

    tracker, writer = FakeTracker(), FakeWriter()
    store = Store(Path(tempfile.mkdtemp()) / "t.db")
    flow = Flow(chat, tracker, writer, store, approvers=[])
    runner = asyncio.create_task(flow.run())
    await asyncio.sleep(0.3)

    async def send(frame, wait=0.3):
        await to_send.put(frame)
        await asyncio.sleep(wait)

    # 1. bot's own message, other channel, and empty text are ignored
    await send(event("X0", "hi", author=BOT))
    await send(event("X1", "hi", channel="OTHER"))
    assert not writer.calls, writer.calls

    # 2. new message -> placeholder reply in thread -> edited into proposal with buttons
    await send(event("S1", "standup notes: fix login, csv export"))
    await send(event("S1", "standup notes: fix login, csv export"))  # duplicate delivery
    assert len(writer.calls) == 1, "duplicate event must be ignored"
    assert writer.calls[0][1] == "Titles must start with a verb.", "SOP must reach the LLM"
    post = rest.calls[2]
    assert post[0] == "POST" and post[1] == f"/v1/channels/{CHANNEL}/messages/S1", post
    edit = rest.calls[-1]
    assert edit[0] == "PUT" and edit[1] == f"/v1/channels/{CHANNEL}/messages/M1", edit
    blocks = edit[2]["blocks"]
    actions = next(b for b in blocks if b["type"] == "actions")["elements"]
    assert [a["onAction"] for a in actions] == [APPROVE, REJECT]
    proposal_id = actions[0]["value"]
    assert "Fix login" in edit[2]["text"]

    # 3. reply in thread -> revision
    await send(event("R1", "merge them into one", thread="S1"))
    assert writer.calls[-1][2] == "merge them into one"
    assert "Merged ticket" in rest.calls[-2][2]["text"], rest.calls[-2]  # proposal edited
    assert "Updated the proposal" in rest.calls[-1][2]["text"]

    # 4. approve with a transient failure -> back to pending with retry button
    tracker.fail_once.add("Merged ticket")
    await send(click("c1", APPROVE, proposal_id))
    assert any(json.loads(m).get("correlation_id") == "c1" for m in received if m != "ping"), "click must be acked"
    assert "failed" in rest.calls[-1][2]["text"]
    assert not tracker.created

    # 5. approve again (double click) -> created exactly once, confirmation with links
    await to_send.put(click("c2", APPROVE, proposal_id))
    await send(click("c3", APPROVE, proposal_id), wait=0.5)
    assert tracker.created == ["Merged ticket"], tracker.created
    confirmation = rest.calls[-1][2]
    assert "Created 1 ticket" in confirmation["text"]
    links = [e for b in confirmation["blocks"] for s in b["elements"] for e in s["elements"] if e["type"] == "link"]
    assert links[0]["url"] == "https://notion.so/Merged-ticket", links
    final_edit = [c for c in rest.calls if c[0] == "PUT"][-1][2]
    assert not [b for b in final_edit["blocks"] if b["type"] == "actions"], "buttons must be removed when done"

    # 6. nothing actionable
    await send(event("S2", "nothing here, just saying hi"))
    assert "Nothing actionable" in rest.calls[-1][2]["text"]

    # 7. reject flow
    await send(event("S3", "another set of notes"))
    pid = next(b for b in rest.calls[-1][2]["blocks"] if b["type"] == "actions")["elements"][1]["value"]
    await send(click("c4", REJECT, pid))
    assert "Rejected" in rest.calls[-1][2]["text"]
    await send(click("c5", APPROVE, pid))
    assert tracker.created == ["Merged ticket"], "rejected proposal must not be approvable"

    # 8. @mention is stripped; replying "approve" in the thread creates the tickets (no buttons needed)
    await send(event("S4", "<<@BOT>> standup notes again"))
    assert writer.calls[-1][0] == "standup notes again", writer.calls[-1]
    await send(event("R4", "Approve!", thread="S4"), wait=0.5)
    assert tracker.created[-2:] == ["Fix login", "Add CSV export"], tracker.created
    assert "Created 2 tickets" in rest.calls[-1][2]["text"]

    # 9. replying "reject" in the thread rejects
    await send(event("S5", "more notes"))
    await send(event("R5", "reject", thread="S5"))
    assert "Rejected" in rest.calls[-1][2]["text"]

    runner.cancel()
    srv.close()
    print("ALL FLOW TESTS PASSED")


asyncio.run(main())
