"""The one place that runs Claude: each call is one run of Claude Code, started by the Agent SDK.

Claude Code is signed in with the owner's Claude plan (`DEALWATCH_CLAUDE_CODE_OAUTH_TOKEN`), so the price checks,
the smart entry and Ask count against the plan's usage limits and nothing is billed per call (docs/decisions.md).
It gets none of its own tools except, when a `WebPolicy` is given, web search and page reading, held to the shops
the policy allows and to its budget. Everything else it may call is the app's own functions, served in process.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import tempfile
from collections.abc import AsyncIterator, Callable
from contextlib import aclosing
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    HookMatcher,
    RateLimitEvent,
    ResultMessage,
    create_sdk_mcp_server,
    query,
)
from claude_agent_sdk import tool as sdk_tool

from ..domains import domain_matches, domain_of_url
from .tool import Tool

log = logging.getLogger(__name__)
SERVER = "dealwatch"
WEB_TOOLS = ["WebSearch", "WebFetch"]
# Its own folder, so nothing from a personal Claude Code setup (settings, memory, sessions) is read, and nothing
# is kept: the conversation lives in chat_messages, the prices in the database.
CLAUDE_HOME = Path(tempfile.gettempdir()) / "dealwatch-claude"
# Inside Claude Code these outrank the plan token and would bill an API or cloud account.
PAID_CREDENTIALS = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
)

LIMIT_REACHED = "Your Claude plan's usage limit is reached{when}. Try again after that."
NOT_SIGNED_IN = "Claude could not sign in to your Claude plan. The token may have expired: see docs/assistant.md."
FAILED = "Claude could not answer just now. Anything it changed before that is saved."
TOO_SLOW = "Claude took too long and was stopped. Anything it changed is saved."
TOO_MANY_STEPS = "Claude needed more steps than allowed and was stopped. Anything it changed is saved."


class Unavailable(Exception):
    """No answer this time. The message is a plain sentence for the person."""


class CannotRun(Unavailable):
    """Nothing more can run for now: the plan's usage limit is reached, or the sign-in to the plan failed."""


@dataclass
class WebPolicy:
    """What Claude may look up on the web in one run. Checked by the app before every search and page read, so
    it holds whatever the model asks for: searches are pinned to the allowed (or away from the blocked) shops,
    pages outside them are refused, and nothing runs once the budget is used."""

    allowed: list[str] = field(default_factory=list)  # only these domains and their subdomains; empty: the web
    blocked: list[str] = field(default_factory=list)
    budget: int = 10
    lookups: list[str] = field(default_factory=list)  # what was searched or read, for the run's log

    def permits(self, domain: str) -> bool:
        if self.allowed:
            return any(domain_matches(domain, allowed) for allowed in self.allowed)
        return bool(domain) and not any(domain_matches(domain, blocked) for blocked in self.blocked)

    def decide(self, tool_name: str, tool_input: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """(the input to run with, "") or (None, why it is refused)."""
        if len(self.lookups) >= self.budget:
            return None, f"The budget of {self.budget} web lookups is used up. Finish with what you found."
        args = dict(tool_input)
        if tool_name == "WebSearch":
            args.pop("allowed_domains", None)
            args.pop("blocked_domains", None)
            if self.allowed:
                args["allowed_domains"] = list(self.allowed)
            elif self.blocked:
                args["blocked_domains"] = list(self.blocked)
            self.lookups.append(f"search: {args.get('query', '')}")
        else:
            url = str(args.get("url", ""))
            if not self.permits(domain_of_url(url)):
                shops = ", ".join(self.allowed) or "shops that are not blocked"
                return None, f"Only pages from these shops may be opened: {shops}."
            self.lookups.append(f"page: {url}")
        return args, ""

    async def hook(self, input_data: dict[str, Any], _tool_use_id: str | None, _context: Any) -> dict[str, Any]:
        """Claude Code's PreToolUse hook around `decide`."""
        args, reason = self.decide(input_data["tool_name"], input_data.get("tool_input") or {})
        if args is None:
            out = {"permissionDecision": "deny", "permissionDecisionReason": reason}
        else:
            out = {"permissionDecision": "allow", "updatedInput": args}
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse", **out}}


def plan_only_env(token: str) -> dict[str, str]:
    """Environment for Claude Code that can only ever use the Claude plan.

    Claude Code inherits this process's environment, so the credentials that would be billed elsewhere are removed
    here, where it cannot be skipped.
    """
    for name in PAID_CREDENTIALS:
        if os.environ.pop(name, None) is not None:
            log.warning("ignoring %s: Dealwatch only uses the Claude plan", name)
    return {"CLAUDE_CODE_OAUTH_TOKEN": token, "CLAUDE_CONFIG_DIR": str(CLAUDE_HOME)}


def adapt(t: Tool, on_error: Callable[[], None] | None = None):
    """One tool in the shape Claude Code calls. A failed call is told to the model, not raised."""

    @sdk_tool(t.name, t.description, t.input_schema)
    async def run(args: dict) -> dict:
        try:
            text, failed = t.call(args), False
        except Exception as e:  # the model reads the reason and corrects itself
            if on_error:
                on_error()
            text, failed = f"Error: {e}", True
        return {"content": [{"type": "text", "text": text}], "is_error": failed}

    return run


def _with_photo(text: str, jpeg: bytes) -> AsyncIterator[dict]:
    """One user message with the photo first and the words after it, so Claude Code sees it without file access."""

    async def one_message():
        source = {"type": "base64", "media_type": "image/jpeg", "data": base64.b64encode(jpeg).decode("ascii")}
        message = {"role": "user", "content": [{"type": "image", "source": source}, {"type": "text", "text": text}]}
        yield {"type": "user", "session_id": "", "message": message, "parent_tool_use_id": None}

    return one_message()


class Claude:
    def __init__(self, token: str, timezone: str = "Europe/Amsterdam") -> None:
        self.token = token
        self.timezone = timezone

    @property
    def enabled(self) -> bool:
        return bool(self.token)

    def ask(
        self,
        text: str,
        system: str,
        *,
        model: str,
        effort: str,
        tools: list[Tool] | None = None,
        photo: bytes | None = None,
        web: WebPolicy | None = None,
        timeout: float = 180,
        max_turns: int = 10,
        on_tool_error: Callable[[], None] | None = None,
    ) -> str:
        """One run of Claude Code, start to finish; returns its final text. `photo` is a JPEG sent with the words.
        Raises Unavailable (CannotRun for the plan's limit or a failed sign-in) with the reason in words."""
        prompt = _with_photo(text, photo) if photo else text
        run = self._run(prompt, system, tools or [], model, effort, web, max_turns, on_tool_error)
        try:
            return asyncio.run(asyncio.wait_for(run, timeout))
        except Unavailable:
            raise
        except TimeoutError:
            raise Unavailable(TOO_SLOW) from None
        except Exception:  # Claude Code missing, crashed, or answered in a way the SDK did not expect
            log.exception("Claude Code could not run")
            raise Unavailable(FAILED) from None

    async def _run(
        self,
        prompt: str | AsyncIterator[dict],
        system: str,
        tools: list[Tool],
        model: str,
        effort: str,
        web: WebPolicy | None,
        max_turns: int,
        on_tool_error: Callable[[], None] | None,
    ) -> str:
        CLAUDE_HOME.mkdir(parents=True, exist_ok=True)
        builtin = WEB_TOOLS if web else []
        with tempfile.NamedTemporaryFile("w", suffix=".md", encoding="utf-8") as prompt_file:
            # A file, not an argument: the item list makes it too long for a command line.
            prompt_file.write(system)
            prompt_file.flush()
            options = ClaudeAgentOptions(
                system_prompt={"type": "file", "path": prompt_file.name},
                tools=builtin,  # no files, no shell; the web only with a policy
                mcp_servers={SERVER: create_sdk_mcp_server(SERVER, tools=[adapt(t, on_tool_error) for t in tools])},
                strict_mcp_config=True,
                allowed_tools=[*builtin, f"mcp__{SERVER}"],
                permission_mode="dontAsk",  # anything not allowed is refused, never asked
                hooks={"PreToolUse": [HookMatcher(matcher="|".join(WEB_TOOLS), hooks=[web.hook])]} if web else None,
                setting_sources=[],  # no settings, CLAUDE.md or hooks from the machine
                max_turns=max_turns,
                model=model,
                effort=effort,
                cwd=CLAUDE_HOME,
                env=plan_only_env(self.token),
                extra_args={"no-session-persistence": None},
            )
            text = ""
            # aclosing: leaving the loop early also stops Claude Code.
            async with aclosing(query(prompt=prompt, options=options)) as messages:
                async for message in messages:
                    if isinstance(message, RateLimitEvent):
                        if message.rate_limit_info.status == "rejected":
                            # Stop here. Going on could only mean paid extra usage, never wanted.
                            raise CannotRun(self._limit_message(message))
                    elif isinstance(message, AssistantMessage):
                        if message.error == "authentication_failed":
                            raise CannotRun(NOT_SIGNED_IN)
                    elif isinstance(message, ResultMessage):
                        if message.subtype == "error_max_turns":
                            raise Unavailable(TOO_MANY_STEPS)
                        if message.is_error:
                            log.error("Claude Code run failed: %s %s", message.subtype, message.errors)
                            raise Unavailable(FAILED)
                        text = (message.result or "").strip()
            return text

    def _limit_message(self, event: RateLimitEvent) -> str:
        resets = event.rate_limit_info.resets_at
        when = f" until {datetime.fromtimestamp(resets, ZoneInfo(self.timezone)):%H:%M}" if resets else ""
        return LIMIT_REACHED.format(when=when)
