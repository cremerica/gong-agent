"""Tool schemas + dispatch for the agent loop.

Every record_* tool echoes back the current accumulated state in its
tool_result, so the model has visibility into what it has already recorded
without needing a separate read-only tool.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from gong_agent.account_data import AccountDataStore
from gong_agent.gong_client import GongAPIError
from gong_agent.state import ROE_ITEMS, ScorecardState, compute_cadence_gap


@dataclass
class ToolContext:
    store: AccountDataStore
    scorecard: ScorecardState


TOOL_SCHEMAS = [
    {
        "name": "list_calls",
        "description": (
            "List Gong calls for this account within a date window, matched by its Salesforce "
            "Account/Opportunity record. The default lookback window is already loaded before "
            "you start - call this again only if you need to widen or narrow that window."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "since_date": {"type": "string", "description": "YYYY-MM-DD, inclusive"},
                "until_date": {"type": "string", "description": "YYYY-MM-DD, inclusive"},
            },
            "required": ["since_date", "until_date"],
        },
    },
    {
        "name": "get_transcript",
        "description": "Fetch the full diarized transcript for one call, with speaker names resolved (e.g. '[Jane Doe] (00:04:12): ...').",
        "input_schema": {
            "type": "object",
            "properties": {"call_id": {"type": "string"}},
            "required": ["call_id"],
        },
    },
    {
        "name": "search_calls",
        "description": (
            "Search across all of this account's calls for a keyword or phrase (a topic, a "
            "name, an objection phrase, a commitment like 'I'll send'). Returns matching "
            "call_id, date, and a short snippet of surrounding context per hit. Use this to "
            "find likely candidates before reading full transcripts."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
    {
        "name": "compute_cadence_gap",
        "description": (
            "Pure calculation, no reasoning needed: days since the last call vs. the "
            "account's expected cadence, plus any historical gap that blew past 2x the "
            "expected cadence during the tracked window."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "record_finding",
        "description": (
            "Record (or update) your grading for one scorecard item. Call once per item when "
            "you've decided its status; call again later if you find better evidence. "
            "evidence_quote and call_date are required when status is 'evidence_found'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "roe_item": {"type": "string", "enum": sorted(ROE_ITEMS)},
                "status": {"type": "string", "enum": ["evidence_found", "gap", "unverifiable"]},
                "evidence_quote": {"type": "string", "description": "Direct quote from the transcript"},
                "call_date": {"type": "string", "description": "YYYY-MM-DD of the call the quote is from"},
                "call_id": {"type": "string"},
            },
            "required": ["roe_item", "status"],
        },
    },
    {
        "name": "record_commitment",
        "description": (
            "Record a follow-up commitment made on a call (e.g. 'I'll send you the security "
            "doc', 'we'll get you access to the staging cluster'). Call again with the same "
            "description and status='confirmed_done' if a later call confirms it was done."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "description": {"type": "string"},
                "made_on_call_date": {"type": "string", "description": "YYYY-MM-DD"},
                "made_by": {"type": "string", "enum": ["se", "prospect"]},
                "status": {"type": "string", "enum": ["open", "confirmed_done"]},
                "confirmed_on_call_date": {"type": "string", "description": "YYYY-MM-DD, required if status=confirmed_done"},
            },
            "required": ["description", "made_on_call_date", "made_by", "status"],
        },
    },
    {
        "name": "finalize_report",
        "description": (
            "Call this once you have looked for evidence on every scorecard item and are done "
            "recording follow-up commitments. Ends the analysis loop."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
]


def dispatch(name: str, tool_input: dict, ctx: ToolContext) -> tuple[str, bool]:
    """Returns (content_str, is_error)."""
    try:
        if name == "list_calls":
            calls = ctx.store.load_calls(tool_input["since_date"], tool_input["until_date"])
            summary = [{"call_id": c.call_id, "date": c.date, "duration_seconds": c.duration_seconds} for c in calls]
            return json.dumps({"count": len(summary), "calls": summary}), False

        if name == "get_transcript":
            formatted = ctx.store.get_transcript(tool_input["call_id"])
            if formatted is None:
                return f"No call found with id {tool_input['call_id']!r} in the currently loaded window.", True
            return json.dumps({"call_id": formatted.call_id, "date": formatted.date, "transcript": formatted.text}), False

        if name == "search_calls":
            results = ctx.store.search(tool_input["query"])
            return json.dumps({"count": len(results), "matches": results}), False

        if name == "compute_cadence_gap":
            gap = compute_cadence_gap(ctx.store.calls, ctx.store.account.expected_cadence_days)
            return json.dumps(gap), False

        if name == "record_finding":
            snapshot = ctx.scorecard.record_finding(
                roe_item=tool_input["roe_item"],
                status=tool_input["status"],
                evidence_quote=tool_input.get("evidence_quote"),
                call_date=tool_input.get("call_date"),
                call_id=tool_input.get("call_id"),
            )
            return json.dumps({"recorded": True, "current_scorecard": snapshot}), False

        if name == "record_commitment":
            commitments = ctx.scorecard.record_commitment(
                description=tool_input["description"],
                made_on_call_date=tool_input["made_on_call_date"],
                made_by=tool_input["made_by"],
                status=tool_input["status"],
                confirmed_on_call_date=tool_input.get("confirmed_on_call_date"),
            )
            return json.dumps({"recorded": True, "current_commitments": commitments}), False

        if name == "finalize_report":
            ctx.scorecard.finalized = True
            return json.dumps({"finalized": True}), False

        return f"Unknown tool '{name}'", True

    except (ValueError, GongAPIError) as e:
        return str(e), True
    except KeyError as e:
        return f"Missing required input field: {e}", True
