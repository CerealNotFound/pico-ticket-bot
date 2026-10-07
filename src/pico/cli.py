"""pico doctor | pico watch | pico try "some messy notes" [--create]"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

from pico.config import ROOT, Settings

log = logging.getLogger("pico")
OK, FAIL = "✅", "❌"


def build(settings: Settings):
    # Swap these two lines to plug in another chat platform / tracker.
    from pico.chat.pumble import PumbleChat
    from pico.trackers.notion import NotionTracker

    chat = PumbleChat(
        settings.pumble_app_id, settings.pumble_app_key, settings.pumble_bot_token, settings.pumble_channel
    )
    tracker = NotionTracker(
        settings.notion_token,
        settings.notion_database,
        sop_page=settings.notion_sop_page,
        sop_file=settings.sop_file,
        default_status=settings.notion_default_status,
        allow_new_labels=settings.notion_allow_new_labels,
        prop_overrides=settings.notion_props,
    )
    return chat, tracker


async def doctor(settings: Settings, skip_llm: bool) -> bool:
    from pico.llm import TicketWriter

    chat, tracker = build(settings)
    healthy = True
    if not (ROOT / ".env").exists():
        print(f"{FAIL} No .env file. Run: cp .env.example .env  and fill it in.")
        return False

    for label, component in (("Notion", tracker), ("Pumble", chat)):
        print(f"\n── {label} " + "─" * 40)
        try:
            await component.setup()
            for line in await component.doctor():
                print(f"   {line}")
            print(f"{OK} {label} OK")
        except Exception as e:
            healthy = False
            print(f"{FAIL} {label}: {e}")

    print("\n── LLM " + "─" * 43)
    try:
        writer = TicketWriter(settings.llm_backend, settings.llm_model)
        print(f"   backend: {writer.name}{f' ({settings.llm_model})' if settings.llm_model else ''}")
        if writer.name == "claude-cli":
            from pico.llm import subscription_status

            print(f"   auth: {subscription_status()}")
        else:
            print("   ⚠️  anthropic backend: each draft is billed to ANTHROPIC_API_KEY")
        if not skip_llm:
            board = tracker.schema() if tracker.title_prop else None
            from pico.models import BoardSchema

            draft = await writer.draft("We need to fix the broken login redirect on Safari.", board or BoardSchema())
            print(f"   test draft: {[t.title for t in draft.tickets]}")
        print(f"{OK} LLM OK")
    except Exception as e:
        healthy = False
        print(f"{FAIL} LLM: {e}")

    print(
        "\n" + (f"{OK} All good. Start the bot with: uv run pico watch" if healthy else f"{FAIL} Fix the issues above.")
    )
    return healthy


async def watch(settings: Settings) -> None:
    from pico.flow import Flow
    from pico.llm import TicketWriter
    from pico.store import Store

    chat, tracker = build(settings)
    await tracker.setup()
    for w in getattr(tracker, "warnings", []):
        log.warning(w)
    await chat.setup()
    writer = TicketWriter(settings.llm_backend, settings.llm_model)
    if writer.name == "claude-cli":
        from pico.llm import subscription_status

        log.info(subscription_status())
    flow = Flow(chat, tracker, writer, Store(settings.db_path), settings.pumble_approver_ids)
    log.info(
        "Pico is up: #%s (%s) → %s board '%s' using %s. Ctrl+C to stop.",
        chat.channel_name, chat.name, tracker.name, getattr(tracker, "board_name", ""), writer.name,
    )  # fmt: skip
    await flow.run()


async def try_it(settings: Settings, text: str, create: bool) -> None:
    """Run the LLM (and optionally Notion) on some text without Pumble."""
    from pico.llm import TicketWriter
    from pico.models import BoardSchema

    _, tracker = build(settings)
    try:
        await tracker.setup()
        board = tracker.schema()
    except Exception as e:
        if create:
            raise
        print(f"(no board: {e} - drafting without board fields)")
        board = BoardSchema()
    writer = TicketWriter(settings.llm_backend, settings.llm_model)
    draft = await writer.draft(text, board, await tracker.standards())
    print(f"\n{draft.summary}\n")
    for i, t in enumerate(draft.tickets, 1):
        print(f"{i}. {t.title}  [{t.priority or '-'}] {t.labels or ''} {('@' + t.assignee) if t.assignee else ''}")
        print(f"   {t.description}")
        for c in t.acceptance_criteria:
            print(f"   ☐ {c}")
        if create:
            print(f"   → {await tracker.create(t)}")
        print()


def main() -> None:
    parser = argparse.ArgumentParser(prog="pico", description="Pumble → Claude → Notion ticket bot")
    sub = parser.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("doctor", help="check every connection and show what the bot will do")
    d.add_argument("--skip-llm", action="store_true", help="don't make a test LLM call")
    sub.add_parser("watch", help="run the bot (listens to Pumble until Ctrl+C)")
    t = sub.add_parser("try", help="draft tickets from text without Pumble")
    t.add_argument("text", help="the messy message (use - to read stdin)")
    t.add_argument("--create", action="store_true", help="also create the tickets in Notion")
    args = parser.parse_args()
    settings = Settings.load()  # loads .env first, so PICO_DEBUG can live there

    logging.basicConfig(
        level=logging.DEBUG if os.getenv("PICO_DEBUG") else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    for noisy in ("httpx", "httpcore", "websockets", "notion_client", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    try:
        if args.cmd == "doctor":
            sys.exit(0 if asyncio.run(doctor(settings, args.skip_llm)) else 1)
        elif args.cmd == "watch":
            asyncio.run(watch(settings))
        elif args.cmd == "try":
            text = sys.stdin.read() if args.text == "-" else args.text
            asyncio.run(try_it(settings, text, args.create))
    except KeyboardInterrupt:
        print("\nbye")
    except RuntimeError as e:
        print(f"{FAIL} {e}")
        sys.exit(1)
