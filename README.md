# Pico

Pumble message → Claude drafts tickets → you approve in Pumble → tickets land on your Notion board → links posted back.

Runs on your laptop. No server, no public URL, and (by default) no API costs: it uses the Claude Code you're already
logged in to.

```
#tickets:  "standup: safari login redirect broke again, also customers want CSV export…"
  └─ Pico (thread): 📝 Proposed 2 tickets …
       you: "make the first one P0"          ← optional, revises the proposal
       Pico: ✏️ Updated the proposal above.
       you: "approve"
       Pico: ✅ Created 2 tickets in Notion  • Fix Safari login redirect  • Add CSV export  · open board
```

## Requirements
- [uv](https://docs.astral.sh/uv/) (Python package manager)
- [Claude Code](https://code.claude.com) installed and logged in (`claude` → `/login`) - or an Anthropic API key
- A Pumble workspace where you're an admin, and a Notion workspace

```bash
git clone <this repo> && cd <repo>
uv sync
cp .env.example .env   # then fill it in - every setting is explained inside
```

## Run

```bash
uv run pico doctor     # checks Pumble, Notion, the LLM, and shows the column mapping it will use
uv run pico watch      # the bot. Ctrl+C to stop
uv run pico try "some messy notes" [--create]   # test drafting (and Notion) without Pumble
```

Or from Claude Code in this folder: `/pico` starts it in the background and keeps an eye on it.

## One-time setup

### 1. Pumble app (workspace admin, ~5 min)
1. Pumble → workspace name menu → **Workspace settings → Configure apps → + Create app → Create from scratch**. Name it `Pico`.
2. Turn **Socket mode ON** (this is why no public URL / ngrok is needed).
3. **Bot scopes:** `messages:read`, `messages:write`, `messages:edit`, `channels:read`.
4. **Events:** `NEW_MESSAGE`.
5. **Install** the app, then copy into `.env`: **App ID** → `PUMBLE_APP_ID`, **App key** → `PUMBLE_APP_KEY`, **Bot token** → `PUMBLE_BOT_TOKEN`.
6. Create a `#tickets` channel (or use any channel; set `PUMBLE_CHANNEL`) and **add the Pico bot to it**.
7. If you change scopes after installing, **reinstall the app and copy the new bot token**; the old token keeps the old
   scopes. `PUMBLE_CHANNEL` also accepts a channel link (right-click the channel → Copy link), which needs no extra scope.

> If the UI doesn't show an App ID, create the app with Pumble's CLI instead (`npx pumble-cli login`, then
> `npx pumble-cli create`) - it writes `PUMBLE_APP_ID` / `PUMBLE_APP_KEY` into `.pumbleapprc`; set `"socketMode": true`
> and the scopes/events above in its `manifest.json`.

### 2. Notion connection (~3 min)
1. Go to <https://app.notion.com/developers/connections> → **Internal connections → Create a new connection** → name `Pico`.
2. Capabilities: **Read content**, **Insert content**, and **Read user information** (needed to set assignees).
3. Copy the token (`ntn_…`) → `NOTION_TOKEN`.
4. Open your board in Notion → **••• → Connections → add Pico**. Copy the board's URL → `NOTION_DATABASE`.
5. Optional: put your ticket standards in a Notion page, share it with Pico the same way, URL → `NOTION_SOP_PAGE`
   (or copy `sop.example.md` to `sop.md` and edit it).

The bot reads the board's columns itself: it finds the title, a `Priority` select, a `Tags/Labels` multi-select, a
`Status`, and an `Assignee` people column, and only ever uses values that already exist. `pico doctor` prints the
mapping; override with `NOTION_PROP_*` if it guesses wrong. Set `NOTION_DEFAULT_STATUS` (e.g. `Backlog`) to choose the
column new tickets land in.

### 3. LLM
Default `LLM_BACKEND=claude-cli` runs `claude -p` with your Claude Code login - nothing to set up. For an always-on team
bot, use `LLM_BACKEND=anthropic` with `ANTHROPIC_API_KEY` (defaults to Claude Sonnet 5.5; server-side fallback on
refusals is enabled).

### 4. Go
```bash
cp .env.example .env   # fill it in
uv run pico doctor
uv run pico watch
```
Post a messy message in the channel.

## How it behaves
- Every top-level message in the watched channel is treated as input (an `@Pico` mention is fine but not needed).
- In the proposal's thread, reply **`approve`** (or yes / ok / lgtm / 👍) to create the tickets, **`reject`** (or no /
  cancel) to drop them, or anything else to revise the proposal.
- The proposal also has Approve/Reject buttons. Pumble currently doesn't deliver button clicks to apps created from its
  settings page, so thread replies are the reliable way; the buttons work for apps created with `pumble-cli`.
- Approve/Reject can be limited to specific people with `PUMBLE_APPROVER_IDS`.
- Double clicks and duplicate events are harmless: approval is an atomic state change in `pico.db` (SQLite).
- If a Notion create fails, the tickets that succeeded keep their links, the error is posted in the thread, and the
  proposal goes back to pending - reply `approve` again to retry just the failed ones.
- Websocket drops reconnect automatically. `PICO_DEBUG=1` logs every raw Pumble frame.

## Extending
- `src/pico/chat/base.py` - `ChatProvider` interface (Pumble lives in `chat/pumble.py`). Add `chat/slack.py`.
- `src/pico/trackers/base.py` - `Tracker` interface (Notion lives in `trackers/notion.py`). Add `trackers/linear.py`.
- Wire the new one in `build()` in `src/pico/cli.py`. `flow.py` doesn't change.

## Tests
```bash
uv run python tests/test_flow.py     # full flow against a fake Pumble websocket + REST
uv run python tests/test_notion.py   # Notion schema detection + page payloads against a mocked API
```

## Costs
With `LLM_BACKEND=claude-cli` (default) drafts run through your Claude Code subscription, so there's no per-call cost;
the bot removes any `ANTHROPIC_API_KEY` from the environment before calling `claude` and refuses to start unless
`claude` is logged in with a claude.ai account. Subscription limits assume ordinary, individual use - if your whole
team posts in the channel, switch to `LLM_BACKEND=anthropic` with an API key (about 1-2 cents per message).
Notion and Pumble API calls are free.
