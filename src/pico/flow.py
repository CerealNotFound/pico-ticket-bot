"""The whole business logic: message -> proposal -> approve/reject/revise -> tickets -> confirmation."""

from __future__ import annotations

import logging

from pico.chat.base import ChatProvider
from pico.llm import TicketWriter
from pico.models import Button, ButtonClick, IncomingMessage, OutMessage, Span, Ticket
from pico.store import Proposal, Store
from pico.trackers.base import Tracker

log = logging.getLogger(__name__)

APPROVE, REJECT = "pico_approve", "pico_reject"

APPROVE_WORDS = {
    "approve",
    "approved",
    "yes",
    "y",
    "ok",
    "okay",
    "lgtm",
    "ship it",
    "go",
    "create",
    "create them",
    "👍",
    "✅",
    ":+1:",
    ":thumbsup:",
    ":white_check_mark:",
    ":heavy_check_mark:",
}
REJECT_WORDS = {
    "reject",
    "rejected",
    "no",
    "n",
    "cancel",
    "discard",
    "drop it",
    "❌",
    "👎",
    ":x:",
    ":-1:",
    ":thumbsdown:",
}


def reply_intent(text: str) -> str | None:
    """A short thread reply that is clearly approve/reject; anything else is revision feedback."""
    t = text.strip().lower().rstrip(".!")
    if t in APPROVE_WORDS:
        return APPROVE
    if t in REJECT_WORDS:
        return REJECT
    return None


class Flow:
    def __init__(self, chat: ChatProvider, tracker: Tracker, writer: TicketWriter, store: Store, approvers: list[str]):
        self.chat = chat
        self.tracker = tracker
        self.writer = writer
        self.store = store
        self.approvers = approvers

    async def run(self) -> None:
        await self.chat.run(self.on_message, self.on_click)

    # --- incoming messages ---------------------------------------------------------

    async def on_message(self, msg: IncomingMessage) -> None:
        if not msg.text.strip() or not self.store.first_time(msg.id):
            return
        if msg.thread_root_id:
            # A reply in a proposal's thread: "approve" / "reject", or feedback to revise it.
            proposal = self.store.by_thread(msg.thread_root_id)
            if proposal and proposal.status == "pending":
                intent = reply_intent(msg.text)
                if intent:
                    await self.decide(proposal, msg.user_id, intent)
                else:
                    await self.revise(proposal, msg)
            return
        await self.propose(msg)

    async def propose(self, msg: IncomingMessage) -> None:
        log.info("New message %s: drafting tickets", msg.id)
        reply_id = await self.chat.post(OutMessage.plain("⏳ Reading this and drafting tickets…"), msg.id)
        try:
            draft = await self.writer.draft(msg.text, self.tracker.schema(), await self.tracker.standards())
        except Exception as e:
            log.exception("Drafting failed")
            await self.chat.edit(reply_id, OutMessage.plain(f"⚠️ Couldn't draft tickets: {e}"))
            return
        if not draft.tickets:
            await self.chat.edit(reply_id, OutMessage.plain(f"🤷 Nothing actionable found. ({draft.summary})"))
            return
        proposal = self.store.create(msg.channel_id, msg.id, reply_id, msg.text, draft.summary, draft.tickets)
        await self.chat.edit(reply_id, render_proposal(proposal))
        log.info("Proposal %s: %d tickets awaiting approval", proposal.id, len(draft.tickets))

    async def revise(self, proposal: Proposal, msg: IncomingMessage) -> None:
        log.info("Revising proposal %s", proposal.id)
        if not self.store.transition(proposal.id, "pending", "revising"):
            return
        await self.chat.edit(proposal.proposal_message_id, OutMessage.plain("⏳ Revising the proposal…"))
        try:
            draft = await self.writer.draft(
                proposal.source_text,
                self.tracker.schema(),
                await self.tracker.standards(),
                previous=[t for t in proposal.tickets if not t.url],
                feedback=msg.text,
            )
        except Exception as e:
            log.exception("Revision failed")
            self.store.transition(proposal.id, "revising", "pending")
            await self.chat.edit(proposal.proposal_message_id, render_proposal(proposal))
            await self.chat.post(OutMessage.plain(f"⚠️ Couldn't revise: {e}"), proposal.source_message_id)
            return
        # Keep tickets that were already created (after a partial failure) instead of dropping them.
        created = [t for t in proposal.tickets if t.url]
        proposal.tickets = created + draft.tickets
        proposal.summary = draft.summary
        self.store.save_tickets(proposal.id, proposal.tickets, draft.summary)
        self.store.transition(proposal.id, "revising", "pending")
        await self.chat.edit(proposal.proposal_message_id, render_proposal(proposal))
        await self.chat.post(OutMessage.plain("✏️ Updated the proposal above."), proposal.source_message_id)

    # --- buttons ----------------------------------------------------------------------

    async def on_click(self, click: ButtonClick) -> None:
        if click.action not in (APPROVE, REJECT):
            return
        proposal = self.store.get(click.value)
        if not proposal:
            log.warning("Click for unknown proposal %r", click.value)
            return
        await self.decide(proposal, click.user_id, click.action)

    async def decide(self, proposal: Proposal, user_id: str, action: str) -> None:
        """Approve/reject from a button click or a thread reply."""
        if self.approvers and user_id not in self.approvers:
            await self.chat.post(
                OutMessage.plain("🔒 Only the configured approvers can do that."), proposal.source_message_id
            )
            return

        if action == REJECT:
            if self.store.transition(proposal.id, "pending", "rejected"):
                proposal.status = "rejected"
                await self.chat.edit(proposal.proposal_message_id, render_proposal(proposal, footer="❌ Rejected."))
            return

        # Approve. The compare-and-set makes double clicks / duplicate deliveries harmless.
        if not self.store.transition(proposal.id, "pending", "creating"):
            return
        await self.approve(proposal)

    async def approve(self, proposal: Proposal) -> None:
        todo = [t for t in proposal.tickets if not t.url]
        log.info("Proposal %s approved: creating %d tickets", proposal.id, len(todo))
        await self.chat.edit(
            proposal.proposal_message_id,
            render_proposal(
                proposal, footer=f"⏳ Creating {len(todo)} ticket(s) in {self.tracker.name}…", buttons=False
            ),
        )
        errors: list[str] = []
        for ticket in todo:
            try:
                ticket.url = await self.tracker.create(ticket)
                self.store.save_tickets(proposal.id, proposal.tickets)  # persist each URL immediately
            except Exception as e:
                log.exception("Creating %r failed", ticket.title)
                errors.append(f"{ticket.title}: {e}")

        created = [t for t in proposal.tickets if t.url]
        if errors:
            self.store.transition(proposal.id, "creating", "pending")
            proposal.status = "pending"
            await self.chat.edit(
                proposal.proposal_message_id,
                render_proposal(proposal, footer=f"⚠️ {len(errors)} ticket(s) failed. Fix and press Approve to retry."),
            )
            await self.chat.post(
                OutMessage.plain("⚠️ Some tickets failed:\n" + "\n".join(f"• {e}" for e in errors)),
                proposal.source_message_id,
            )
            return

        self.store.transition(proposal.id, "creating", "done")
        proposal.status = "done"
        await self.chat.edit(proposal.proposal_message_id, render_proposal(proposal, footer="✅ Approved."))
        await self.chat.post(render_confirmation(created, self.tracker), proposal.source_message_id)
        log.info("Proposal %s done", proposal.id)


# --- rendering (platform-neutral; each chat adapter turns this into its own format) -------


def ticket_meta(t: Ticket) -> str:
    bits = [b for b in (t.priority, ", ".join(t.labels), f"@{t.assignee}" if t.assignee else "") if b]
    return f"  ·  {'  ·  '.join(bits)}" if bits else ""


def render_proposal(p: Proposal, footer: str | None = None, buttons: bool = True) -> OutMessage:
    n = len(p.tickets)
    lines = [[Span(f"📝 Proposed {n} ticket{'s' if n != 1 else ''}", bold=True), Span(f" - {p.summary}")], []]
    for i, t in enumerate(p.tickets, 1):
        title = Span(t.title, bold=True, url=t.url) if t.url else Span(t.title, bold=True)
        lines.append([Span(f"{i}. "), title, Span(ticket_meta(t)), *([Span("  ✅")] if t.url else [])])
        desc = t.description.strip().replace("\n", " ")
        lines.append([Span(f"     {desc[:280]}{'…' if len(desc) > 280 else ''}", italic=True)])
        for c in t.acceptance_criteria[:5]:
            lines.append([Span(f"     ☐ {c}")])
        if len(t.acceptance_criteria) > 5:
            lines.append([Span(f"     … +{len(t.acceptance_criteria) - 5} more")])
        lines.append([])
    if footer:
        lines.append([Span(footer, bold=True)])
    show_buttons = buttons and p.status == "pending"
    if show_buttons:
        lines.append(
            [
                Span(
                    'Reply "approve" or "reject" in this thread, or describe changes '
                    '(e.g. "merge 1 and 2, make 3 high priority").',
                    italic=True,
                )
            ]
        )
    remaining = sum(1 for t in p.tickets if not t.url)
    btns = (
        [
            Button(f"Approve {'all' if remaining == n else remaining}", APPROVE, p.id, "primary"),
            Button("Reject", REJECT, p.id, "danger"),
        ]
        if show_buttons
        else []
    )
    return OutMessage(lines=lines, buttons=btns)


def render_confirmation(created: list[Ticket], tracker: Tracker) -> OutMessage:
    lines = [[Span(f"✅ Created {len(created)} ticket{'s' if len(created) != 1 else ''} in {tracker.name}", bold=True)]]
    for t in created:
        lines.append([Span("• "), Span(t.title, url=t.url)])
    if tracker.board_url:
        lines.append([Span("Board: "), Span("open board", url=tracker.board_url)])
    return OutMessage(lines=lines)
