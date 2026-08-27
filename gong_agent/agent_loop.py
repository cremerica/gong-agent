"""The actual agentic loop: a hand-rolled while-loop over the raw Messages
API (client.beta.messages.create, needed only for the context-management
beta - not the SDK's beta Tool Runner). Chosen deliberately over the Tool
Runner so the turn cap and the custom finalize_report stop condition are
fully visible and controlled here rather than abstracted away - see the
plan doc's "Agent loop" section for the rationale.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

import anthropic

from gong_agent.account_data import AccountDataStore
from gong_agent.config import AccountConfig
from gong_agent.prompt import MAX_TURNS, SYSTEM_PROMPT
from gong_agent.state import ScorecardState
from gong_agent.tools import TOOL_SCHEMAS, ToolContext, dispatch

MODEL = "claude-sonnet-5"
CONTEXT_MANAGEMENT_BETA = "context-management-2025-06-27"
# Sonnet 5 runs adaptive thinking by default (no "thinking" param needed to
# enable it), and thinking tokens count against max_tokens. 4096 was too
# tight - a turn with substantial reasoning could exhaust the whole budget
# before emitting any usable tool_use/text, surfacing as stop_reason
# "max_tokens" with nothing recorded (confirmed live against a real account).
MAX_TOKENS_PER_TURN = 8192


@dataclass
class RunResult:
    scorecard: ScorecardState
    store: AccountDataStore
    turn_count: int
    finalized: bool
    final_stop_reason: str | None
    usage_per_turn: list[dict] = field(default_factory=list)


def _initial_user_message(account: AccountConfig, call_count: int, since_date: str, until_date: str) -> str:
    return (
        f"Analyze the account \"{account.account_name}\" against the ROE. This account was matched "
        "via its Salesforce Account/Opportunity record (currently in the \"Evaluation POC\" stage), "
        "attached to its calls through Gong's CRM integration.\n"
        f"- Call history window already loaded: {since_date} through {until_date} "
        f"({call_count} call(s) found)\n"
        f"- Expected sync-call cadence: every {account.expected_cadence_days} day(s)\n\n"
        "Begin your analysis. Use search_calls and get_transcript to gather evidence, "
        "compute_cadence_gap for the cadence math, and record your findings and any open "
        "follow-up commitments as you go. Call finalize_report when done."
    )


def run_agent(store: AccountDataStore, since_date: str, until_date: str,
              anthropic_client: anthropic.Anthropic | None = None) -> RunResult:
    """Run the agent loop against a store whose calls have already been
    loaded (via store.seed_calls, from discovery, or store.load_calls) by
    the caller - see main.py."""
    client = anthropic_client or anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    account = store.account
    scorecard = ScorecardState()
    ctx = ToolContext(store=store, scorecard=scorecard)

    messages = [{"role": "user", "content": _initial_user_message(account, len(store.calls), since_date, until_date)}]
    usage_per_turn = []
    turn_count = 0
    final_stop_reason = None

    for turn in range(1, MAX_TURNS + 1):
        turn_count = turn
        response = client.beta.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS_PER_TURN,
            betas=[CONTEXT_MANAGEMENT_BETA],
            system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
            tools=TOOL_SCHEMAS,
            messages=messages,
            output_config={"effort": "medium"},
            context_management={"edits": [{"type": "clear_tool_uses_20250919"}]},
        )
        usage_per_turn.append({
            "turn": turn,
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
            "cache_read_input_tokens": getattr(response.usage, "cache_read_input_tokens", 0),
            "cache_creation_input_tokens": getattr(response.usage, "cache_creation_input_tokens", 0),
        })
        messages.append({"role": "assistant", "content": response.content})
        final_stop_reason = response.stop_reason

        if response.stop_reason != "tool_use":
            # Model ended its turn without calling a tool - either it called
            # finalize_report on a prior turn (handled below) or it stopped
            # unexpectedly. Either way, nothing more to feed back.
            break

        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                content_str, is_error = dispatch(block.name, block.input, ctx)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": content_str,
                    "is_error": is_error,
                })
        messages.append({"role": "user", "content": tool_results})

        if scorecard.finalized:
            break

    return RunResult(
        scorecard=scorecard,
        store=store,
        turn_count=turn_count,
        finalized=scorecard.finalized,
        final_stop_reason=final_stop_reason,
        usage_per_turn=usage_per_turn,
    )
