"""The one place that runs Claude Code, with Claude Code replaced by a scripted stand-in."""

import asyncio
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from claude_agent_sdk import AssistantMessage, CLINotFoundError, RateLimitEvent, RateLimitInfo, TextBlock
from conftest import result

from dealwatch.services.claude import FAILED, TOO_MANY_STEPS, CannotRun, Claude, Unavailable, WebPolicy, adapt
from dealwatch.services.tool import tool


def ask(**kwargs) -> str:
    return Claude("plan-token").ask("hello", "be brief", model="haiku", effort="low", **kwargs)


def test_a_run_uses_only_the_plan_and_the_apps_tools(claude_code, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "would-be-billed")
    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")
    seen = claude_code(result("hi there"))
    assert ask() == "hi there"
    options = seen["options"]
    assert options.tools == [], "no files, no shell and no web without a policy"
    assert options.allowed_tools == ["mcp__dealwatch"] and options.permission_mode == "dontAsk"
    assert options.setting_sources == [] and options.strict_mcp_config is True and options.hooks is None
    assert options.env["CLAUDE_CODE_OAUTH_TOKEN"] == "plan-token"
    assert "ANTHROPIC_API_KEY" not in os.environ and "CLAUDE_CODE_USE_BEDROCK" not in os.environ
    assert (options.model, options.effort) == ("haiku", "low") and seen["system"] == "be brief"


def test_with_a_web_policy_only_search_and_page_reading_are_added(claude_code):
    seen = claude_code(result("done"))
    ask(web=WebPolicy(allowed=["ah.nl"], budget=2))
    options = seen["options"]
    assert options.tools == ["WebSearch", "WebFetch"]
    assert options.allowed_tools == ["WebSearch", "WebFetch", "mcp__dealwatch"]
    assert options.hooks["PreToolUse"][0].matcher == "WebSearch|WebFetch"


def test_the_web_policy_pins_searches_refuses_other_shops_and_holds_the_budget():
    web = WebPolicy(allowed=["ah.nl", "bol.com"], budget=3)
    args, _ = web.decide("WebSearch", {"query": "page keukenpapier", "blocked_domains": ["ah.nl"]})
    assert args == {"query": "page keukenpapier", "allowed_domains": ["ah.nl", "bol.com"]}
    assert web.decide("WebFetch", {"url": "https://www.ah.nl/producten/1", "prompt": "price"})[0] is not None
    refused, why = web.decide("WebFetch", {"url": "https://evil.example/p", "prompt": "price"})
    assert refused is None and "ah.nl, bol.com" in why
    web.decide("WebSearch", {"query": "third"})
    over, why = web.decide("WebSearch", {"query": "fourth"})
    assert over is None and "budget of 3" in why
    assert web.lookups == ["search: page keukenpapier", "page: https://www.ah.nl/producten/1", "search: third"]

    open_web = WebPolicy(blocked=["kruidvat.nl"], budget=5)
    assert open_web.decide("WebSearch", {"query": "x"})[0]["blocked_domains"] == ["kruidvat.nl"]
    assert open_web.decide("WebFetch", {"url": "https://shop.kruidvat.nl/p"})[0] is None
    assert open_web.decide("WebFetch", {"url": "https://www.bol.com/p"})[0] is not None


def test_the_hook_answers_in_claude_codes_shape():
    web = WebPolicy(allowed=["ah.nl"], budget=1)
    allow = asyncio.run(web.hook({"tool_name": "WebSearch", "tool_input": {"query": "q"}}, "t1", None))
    assert allow["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert allow["hookSpecificOutput"]["updatedInput"]["allowed_domains"] == ["ah.nl"]
    deny = asyncio.run(web.hook({"tool_name": "WebSearch", "tool_input": {"query": "q"}}, "t2", None))
    assert deny["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_a_reached_plan_limit_stops_the_run(claude_code):
    resets = int(datetime(2026, 10, 14, 15, 0, tzinfo=ZoneInfo("Europe/Amsterdam")).timestamp())
    limit = RateLimitEvent(rate_limit_info=RateLimitInfo(status="rejected", resets_at=resets), uuid="u", session_id="t")
    claude_code(limit, result("never shown"))
    with pytest.raises(CannotRun, match="usage limit is reached until 15:00"):
        ask()


def test_failures_become_plain_sentences(claude_code):
    claude_code(AssistantMessage(content=[TextBlock("401")], model="x", error="authentication_failed"))
    with pytest.raises(CannotRun, match="could not sign in to your Claude plan"):
        ask()
    claude_code(result(None, is_error=True))
    with pytest.raises(Unavailable, match=FAILED):
        ask()
    claude_code(result(None, is_error=True, subtype="error_max_turns"))
    with pytest.raises(Unavailable, match=TOO_MANY_STEPS):
        ask()
    for crash in (CLINotFoundError(), RuntimeError("unexpected")):
        claude_code(raises=crash)
        with pytest.raises(Unavailable, match=FAILED):
            ask()


def test_a_failing_tool_is_told_to_the_model_not_raised():
    rolled_back = []

    @tool
    def needs_a_number(n: int) -> str:
        """Doubles it.

        Args:
            n: A number.
        """
        return str(n * 2)

    handler = adapt(needs_a_number, lambda: rolled_back.append(True)).handler
    assert asyncio.run(handler({"n": 2}))["content"][0]["text"] == "4"
    bad = asyncio.run(handler({"n": "two"}))
    assert bad["is_error"] is True and bad["content"][0]["text"].startswith("Error:") and rolled_back == [True]
