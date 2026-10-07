"""Turns a messy message into a validated list of tickets.

Two interchangeable backends:
  - claude-cli: shells out to `claude -p` (uses your Claude Code login, no API key)
  - anthropic:  Anthropic API (needs ANTHROPIC_API_KEY)
Both use JSON-schema structured output, so the result always parses.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile

from pico.models import BoardSchema, Draft, Ticket

SYSTEM_PROMPT = """\
You turn messy engineering chat messages (meeting notes, brain dumps, summaries, \
"we need to..." paragraphs) into clear, actionable tickets for a task board.

Rules:
- One ticket per distinct piece of work. Don't split trivially, don't merge unrelated work.
- Skip anything that is not work for engineers (FYIs, chit-chat, decisions with no follow-up).
- Title: short, imperative, specific ("Add CSV export to reports page"), no prefixes like "Task:".
- Description: the context an engineer needs, in plain prose. Only include facts from the message; \
never invent requirements, dates, names or numbers.
- Acceptance criteria: concrete, checkable outcomes. Leave empty if the message gives nothing to go on.
- priority / labels / assignee: only use the allowed values you are given; use null / [] when unsure.
- If nothing is actionable, return an empty tickets list.
- summary: one short sentence describing what you extracted.
- If team ticket standards are provided below, they override these defaults."""


def build_schema(board: BoardSchema) -> dict:
    def nullable(values: list[str]) -> dict:
        if values:
            return {"anyOf": [{"type": "string", "enum": values}, {"type": "null"}]}
        return {"type": ["string", "null"]}

    label_item = {"type": "string"}
    if board.labels and not board.allow_new_labels:
        label_item["enum"] = board.labels

    ticket = {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "description": {"type": "string"},
            "acceptance_criteria": {"type": "array", "items": {"type": "string"}},
            "priority": nullable(board.priorities),
            "labels": {"type": "array", "items": label_item},
            "assignee": nullable(board.assignees),
        },
        "required": ["title", "description", "acceptance_criteria", "priority", "labels", "assignee"],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {"summary": {"type": "string"}, "tickets": {"type": "array", "items": ticket}},
        "required": ["summary", "tickets"],
        "additionalProperties": False,
    }


def build_system(standards: str | None) -> str:
    if not standards:
        return SYSTEM_PROMPT
    return f"{SYSTEM_PROMPT}\n\n<team_ticket_standards>\n{standards.strip()}\n</team_ticket_standards>"


def build_user_prompt(
    text: str, board: BoardSchema, previous: list[Ticket] | None = None, feedback: str | None = None
) -> str:
    parts = ["Board fields:"]
    parts.append(f"- priorities: {', '.join(board.priorities) or '(none - use null)'}")
    if board.allow_new_labels:
        parts.append(f"- labels (existing; new ones allowed): {', '.join(board.labels) or '(none yet)'}")
    else:
        parts.append(f"- labels: {', '.join(board.labels) or '(none - use [])'}")
    parts.append(f"- assignees: {', '.join(board.assignees) or '(none - use null)'}")
    parts.append(f"\n<message>\n{text.strip()}\n</message>")
    if previous is not None:
        prev = [t.model_dump(exclude={"url"}) for t in previous]
        parts.append(f"\nYou already proposed these tickets:\n{json.dumps(prev, indent=2)}")
        parts.append(
            f"\nThe reviewer replied with this feedback:\n<feedback>\n{feedback}\n</feedback>\n"
            "Return the full revised ticket list, applying the feedback and keeping everything else."
        )
    return "\n".join(parts)


class LLMError(Exception):
    pass


# Env vars that would make `claude` bill an API account instead of using the Claude Code subscription login.
BILLED_AUTH_VARS = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
    "ANTHROPIC_BASE_URL",
)


def subscription_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k not in BILLED_AUTH_VARS}


def subscription_status() -> str:
    """Confirms `claude` is logged in with a Claude subscription (no per-call billing). Raises otherwise."""
    import subprocess

    exe = shutil.which("claude")
    if not exe:
        raise LLMError("`claude` CLI not found on PATH.")
    out = subprocess.run([exe, "auth", "status"], capture_output=True, text=True, env=subscription_env(), timeout=30)
    try:
        status = json.loads(out.stdout)
    except json.JSONDecodeError:
        raise LLMError(f"Couldn't read `claude auth status`: {(out.stderr or out.stdout)[:200]}")
    if not status.get("loggedIn") or status.get("authMethod") != "claude.ai":
        raise LLMError(
            f"`claude` is not logged in with a Claude subscription (authMethod={status.get('authMethod')}). "
            "Run `claude` and /login with your claude.ai account."
        )
    return f"Claude Code subscription login ({status.get('subscriptionType')}, {status.get('email')}) - no API billing"


class ClaudeCLI:
    """Runs `claude -p` as a subprocess with no tools; uses the logged-in Claude Code account."""

    def __init__(self, model: str = "", timeout: float = 300):
        self.model = model
        self.timeout = timeout

    async def complete(self, system: str, user: str, schema: dict) -> dict:
        exe = shutil.which("claude")
        if not exe:
            raise LLMError("`claude` CLI not found on PATH. Install Claude Code or set LLM_BACKEND=anthropic.")
        args = [
            exe, "-p",
            "--output-format", "json",
            "--json-schema", json.dumps(schema),
            "--system-prompt", system,
            "--tools", "",
            "--no-session-persistence",
            "--strict-mcp-config",
            "--setting-sources", "",
        ]  # fmt: skip
        if self.model:
            args += ["--model", self.model]
        # Run outside the project so no CLAUDE.md / project settings leak into the prompt.
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=tempfile.gettempdir(),
            env=subscription_env(),
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(user.encode()), self.timeout)
        except TimeoutError:
            proc.kill()
            raise LLMError(f"claude CLI timed out after {self.timeout:.0f}s")
        try:
            result = json.loads(out)
        except json.JSONDecodeError:
            raise LLMError(f"claude CLI failed (exit {proc.returncode}): {(err or out).decode()[-500:]}")
        if result.get("is_error") or "structured_output" not in result:
            raise LLMError(f"claude CLI error: {str(result.get('result') or result)[:500]}")
        return result["structured_output"]


class AnthropicAPI:
    def __init__(self, model: str = ""):
        import anthropic

        self.client = anthropic.AsyncAnthropic()
        self.model = model or "claude-sonnet-5-5"

    async def complete(self, system: str, user: str, schema: dict) -> dict:
        response = await self.client.beta.messages.create(
            model=self.model,
            max_tokens=16000,
            # System prompt + team standards are identical across calls -> cache them.
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user}],
            output_config={"format": {"type": "json_schema", "schema": schema}},
            # Retry on another model automatically if a safety classifier declines.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        if response.stop_reason == "refusal":
            raise LLMError("The model declined this request.")
        if response.stop_reason == "max_tokens":
            raise LLMError("The response was cut off (message too long?).")
        text = next((b.text for b in response.content if b.type == "text"), "")
        return json.loads(text)


class TicketWriter:
    def __init__(self, backend: str, model: str = ""):
        if backend == "anthropic":
            self.backend = AnthropicAPI(model)
        elif backend == "claude-cli":
            self.backend = ClaudeCLI(model)
        else:
            raise ValueError(f"Unknown LLM_BACKEND {backend!r} (use claude-cli or anthropic)")
        self.name = backend

    async def draft(
        self,
        text: str,
        board: BoardSchema,
        standards: str | None = None,
        previous: list[Ticket] | None = None,
        feedback: str | None = None,
    ) -> Draft:
        data = await self.backend.complete(
            build_system(standards), build_user_prompt(text, board, previous, feedback), build_schema(board)
        )
        return Draft.model_validate(data)
