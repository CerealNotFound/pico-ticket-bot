"""Pumble adapter using Socket Mode: the bot dials out over a websocket, so no public URL/tunnel is needed.

Protocol (mirrors the official pumble-sdk's AddonWebsocketListener):
  GET {API}/apps/{appId}/websocket-connection?appKey=...  -> {"url": "wss://..."}
  frames in:  {"payload": {...}, "correlation_id": "..."}   (or the text "pong")
  events (messageType PUMBLE_EVENT/APP_EVENT): payload.body is a JSON *string*; no ack needed
  interactions (BLOCK_INTERACTION, ...): must be acked within 3s with
      {"correlation_id": ..., "status": 200, "message": "ok"}
  keepalive: send the text "ping" every 25s
"""

from __future__ import annotations

import asyncio
import json
import logging
import re

import httpx
import websockets

from pico.chat.base import OnClick, OnMessage
from pico.models import ButtonClick, IncomingMessage, OutMessage

log = logging.getLogger(__name__)

API = "https://api-ga.pumble.com"
PING_EVERY = 25
MENTION = re.compile(r"<<[@#][^>]*>>")  # e.g. "<<@6ac3...>>" when the bot is @mentioned


class PumbleError(RuntimeError):
    pass


class PumbleChat:
    name = "Pumble"

    def __init__(self, app_id: str, app_key: str, bot_token: str, channel: str):
        self.app_id = app_id
        self.app_key = app_key
        self.bot_token = bot_token
        self.channel = channel
        self.channel_id = ""
        self.channel_name = ""
        self.bot_user_id = ""
        self.http = httpx.AsyncClient(
            base_url=API,
            headers={"token": bot_token, "x-app-token": app_key, "content-type": "application/json"},
            timeout=30,
        )
        self.anon = httpx.AsyncClient(base_url=API, timeout=30)  # the socket-url call takes no token
        self._tasks: set[asyncio.Task] = set()

    # --- REST -----------------------------------------------------------------------

    async def _request(self, method: str, url: str, **kw) -> dict | list | None:
        for attempt in range(4):
            r = await self.http.request(method, url, **kw)
            if r.status_code == 429 and attempt < 3:
                await asyncio.sleep(float(r.headers.get("retry-after", 2 ** (attempt + 1))))
                continue
            if r.status_code >= 400:
                raise PumbleError(f"Pumble {method} {url} -> {r.status_code}: {r.text[:300]}")
            return r.json() if r.content else None
        raise AssertionError("unreachable")

    async def setup(self) -> None:
        required = [
            ("PUMBLE_APP_ID", self.app_id),
            ("PUMBLE_APP_KEY", self.app_key),
            ("PUMBLE_BOT_TOKEN", self.bot_token),
        ]
        missing = [name for name, value in required if not value]
        if missing:
            raise PumbleError(f"Missing in .env: {', '.join(missing)}")
        try:
            me = await self._request("GET", "/oauth2/me")
        except PumbleError as e:
            raise PumbleError(f"PUMBLE_BOT_TOKEN / PUMBLE_APP_KEY rejected: {e}") from e
        self.bot_user_id = me["workspaceUserId"]
        self.bot_name = me.get("workspaceUserName", "")
        await self._resolve_channel()

    async def _resolve_channel(self) -> None:
        # A channel id or a copied channel link works without any listing permission.
        ids = re.findall(r"[0-9a-f]{24}", self.channel.lower())
        if ids:
            self.channel_id = ids[-1]
            try:
                info = await self._request("GET", f"/v1/channels/{self.channel_id}")
                self.channel_name = (info.get("channel", info) or {}).get("name") or self.channel_id
            except PumbleError:
                self.channel_name = self.channel_id  # name is cosmetic; posting will verify access
            return
        try:
            channels = await self._request("GET", "/v1/channels") or []
        except PumbleError as e:
            if "403" not in str(e):
                raise
            raise PumbleError(
                "The bot token can't list channels, so PUMBLE_CHANNEL can't be looked up by name. Either:\n"
                "   a) paste the channel link into .env: right-click #" + self.channel + " → Copy link → "
                "PUMBLE_CHANNEL=<link>   (works right away), or\n"
                "   b) reinstall the app (Configure apps → Pico → reinstall) so the token picks up channels:read, "
                "then copy the NEW bot token into PUMBLE_BOT_TOKEN."
            ) from e
        infos = [c.get("channel", c) for c in channels]
        wanted = self.channel.lower()
        match = next((c for c in infos if c["id"] == self.channel or (c.get("name") or "").lower() == wanted), None)
        if not match:
            names = ", ".join(
                sorted(
                    "#" + c["name"] for c in infos if c.get("name") and c.get("channelType") in ("PUBLIC", "PRIVATE")
                )
            )
            raise PumbleError(
                f"The bot can't see channel {self.channel!r}. Add the bot to it in Pumble (channel → Add people → "
                f"your app), or set PUMBLE_CHANNEL to one of: {names or '(none visible)'}"
            )
        self.channel_id = match["id"]
        self.channel_name = match.get("name") or self.channel

    async def post(self, message: OutMessage, thread_root_id: str | None = None) -> str:
        url = f"/v1/channels/{self.channel_id}/messages"
        if thread_root_id:
            url += f"/{thread_root_id}"
        result = await self._request("POST", url, json=render(message))
        return result["id"]

    async def edit(self, message_id: str, message: OutMessage) -> None:
        await self._request("PUT", f"/v1/channels/{self.channel_id}/messages/{message_id}", json=render(message))

    async def doctor(self) -> list[str]:
        url = await self._socket_url()
        return [
            f"Bot: {self.bot_name or self.bot_user_id} (user id {self.bot_user_id})",
            f"Watching channel: #{self.channel_name} ({self.channel_id})",
            f"Socket mode endpoint OK ({url.split('?')[0][:60]}...)",
        ]

    # --- Socket mode -----------------------------------------------------------

    async def _socket_url(self) -> str:
        r = await self.anon.get(f"/apps/{self.app_id}/websocket-connection", params={"appKey": self.app_key})
        if r.status_code >= 400:
            raise PumbleError(
                f"Couldn't open Socket Mode ({r.status_code}: {r.text[:200]}). Check PUMBLE_APP_ID/PUMBLE_APP_KEY "
                "and that Socket mode is enabled for the app."
            )
        return r.json()["url"]

    async def run(self, on_message: OnMessage, on_click: OnClick) -> None:
        delay = 2
        while True:
            try:
                url = await self._socket_url()
                async with websockets.connect(url, ping_interval=None, max_size=None) as ws:
                    log.info("Connected to Pumble (socket mode). Watching #%s", self.channel_name)
                    delay = 2
                    pinger = asyncio.create_task(self._ping(ws))
                    try:
                        async for raw in ws:
                            if raw == "pong":
                                continue
                            await self._handle_frame(ws, raw, on_message, on_click)
                    finally:
                        pinger.cancel()
                log.warning("Pumble closed the connection; reconnecting")
            except asyncio.CancelledError:
                raise
            except Exception as e:  # network blips, auth hiccups: keep the bot alive
                log.warning("Pumble connection problem: %s (retrying in %ss)", e, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)

    async def _ping(self, ws) -> None:
        while True:
            await asyncio.sleep(PING_EVERY)
            await ws.send("ping")

    async def _handle_frame(self, ws, raw: str | bytes, on_message: OnMessage, on_click: OnClick) -> None:
        log.debug("frame: %s", raw[:1000])
        try:
            frame = json.loads(raw)
        except json.JSONDecodeError:
            log.debug("Ignoring non-JSON frame: %r", raw[:100])
            return
        payload = frame.get("payload") or {}
        kind = payload.get("messageType")
        correlation_id = frame.get("correlation_id")

        # Ack anything carrying a correlation id right away (interactions must be acked within 3s).
        if correlation_id:
            await ws.send(json.dumps({"correlation_id": correlation_id, "status": 200, "message": "ok"}))

        if kind in ("PUMBLE_EVENT", "APP_EVENT"):
            if payload.get("eventType") != "NEW_MESSAGE":
                return
            body = payload.get("body")
            body = json.loads(body) if isinstance(body, str) else body or {}
            if body.get("aId") == self.bot_user_id or body.get("eph") or body.get("cId") != self.channel_id:
                return
            msg = IncomingMessage(
                id=body["mId"],
                channel_id=body["cId"],
                user_id=body.get("aId", ""),
                text=MENTION.sub("", body.get("tx") or "").strip(),
                thread_root_id=body.get("trId") or None,
            )
            self._spawn(on_message(msg))
            return

        log.info("Pumble %s received (action=%s)", kind, payload.get("onAction"))

        if kind == "BLOCK_INTERACTION" and payload.get("sourceType") == "MESSAGE":
            value = payload.get("payload")
            try:
                value = json.loads(value).get("value", "") if isinstance(value, str) else (value or {}).get("value", "")
            except (json.JSONDecodeError, AttributeError):
                value = ""
            click = ButtonClick(
                user_id=payload.get("userId", ""),
                channel_id=payload.get("channelId", ""),
                message_id=payload.get("sourceId", ""),
                action=payload.get("onAction", ""),
                value=value,
            )
            self._spawn(on_click(click))

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        task.add_done_callback(_log_crash)


def _log_crash(task: asyncio.Task) -> None:
    if not task.cancelled() and task.exception():
        log.error("Handler crashed", exc_info=task.exception())


def render(message: OutMessage) -> dict:
    """OutMessage -> Pumble blocks (rich_text + actions), with a plain-text fallback."""
    sections = []
    for line in message.lines:
        elements = []
        for span in line:
            style = {k: True for k in ("bold", "italic") if getattr(span, k)}
            if span.url:
                el = {"type": "link", "url": span.url, "text": span.text}
            else:
                el = {"type": "text", "text": span.text}
            if style:
                el["style"] = style
            elements.append(el)
        elements.append({"type": "text", "text": "\n"})
        sections.append({"type": "rich_text_section", "elements": elements})
    blocks: list[dict] = [{"type": "rich_text", "elements": sections}]
    if message.buttons:
        blocks.append(
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": b.label},
                        "onAction": b.action,
                        "value": b.value,
                        "style": b.style,
                    }
                    for b in message.buttons
                ],
            }
        )
    return {"text": message.to_text(), "blocks": blocks}
