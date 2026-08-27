"""Deterministic Markdown report assembly.

The scorecard, commitment list, and header are built entirely from recorded
tool-call state (record_finding / record_commitment / compute_cadence_gap) -
never parsed from the model's own closing prose. This guarantees complete,
structured output regardless of how or when the agent loop ends.
"""

from __future__ import annotations

from datetime import datetime

from gong_agent.agent_loop import RunResult
from gong_agent.config import AccountConfig
from gong_agent.state import ROE_ITEMS, compute_cadence_gap

NOT_ASSESSED = [
    "Agent install / golden-state config validation (ROE Section 2, 8)",
    "RUM / Heap session review (ROE 3.4)",
    "Klaudia Dashboard session review (ROE 3.4)",
    "Cost-optimization simulation & reporting (ROE 3.5)",
    "Klaudia in-product self-checks (ROE Section 4-5)",
    "Weekly Slack / Salesforce reporting (ROE 3.9)",
    "Escalation / ticket handling (ROE Section 6)",
]

STATUS_LABEL = {
    "evidence_found": "✅ Evidence found",
    "gap": "⚠️ Gap",
    "unverifiable": "❔ Unverifiable via Gong",
    "not_evaluated": "⏸️ Not evaluated (run limitation)",
}


def _not_assessed_section() -> str:
    lines = ["## Not Assessed (Outside Gong Scope)", ""]
    lines += [f"- {item}" for item in NOT_ASSESSED]
    return "\n".join(lines)


def _cadence_line(cadence: dict) -> str:
    if cadence["status"] == "no_data":
        return "No calls found - cadence cannot be assessed."
    line = (
        f"Last call {cadence['last_call_date']} ({cadence['days_since_last_call']} day(s) ago), "
        f"expected every {cadence['expected_cadence_days']} day(s) -> "
        f"**{'on track' if cadence['status'] == 'on_track' else 'GAP'}**."
    )
    if cadence["historical_gaps"]:
        gaps = "; ".join(f"{g['from']} -> {g['to']} ({g['days']}d)" for g in cadence["historical_gaps"])
        line += f"\n\nHistorical gaps exceeding 2x cadence during the window: {gaps}"
    return line


def build_zero_calls_report(account: AccountConfig, since_date: str, until_date: str) -> str:
    generated_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        f"# POC Health Report - {account.account_name}",
        "",
        f"Generated: {generated_at}  ",
        f"Window checked: {since_date} to {until_date}  ",
        "Calls analyzed: **0**  ",
        "Cadence status: **no data**",
        "",
        "No calls were found for this account's Salesforce record in this window. This shouldn't "
        "normally happen for a discovered account (discovery only surfaces accounts that already "
        "have a matching call) - if you're seeing this, it likely means the account's calls fell "
        "outside the window on a re-run, or its Opportunity moved out of the tracked POC stage "
        "between discovery and this report.",
        "",
        "## Scorecard",
        "",
    ]
    for item_id, description in ROE_ITEMS.items():
        lines.append(f"### {description}")
        lines.append(f"**{STATUS_LABEL['unverifiable']}** - no calls found in window.")
        lines.append("")
    lines.append("## Follow-up Commitments")
    lines.append("")
    lines.append("None (no calls found).")
    lines.append("")
    lines.append(_not_assessed_section())
    return "\n".join(lines)


def build_report(account: AccountConfig, run_result: RunResult, since_date: str, until_date: str) -> str:
    generated_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    cadence = compute_cadence_gap(run_result.store.calls, account.expected_cadence_days)

    lines = [
        f"# POC Health Report - {account.account_name}",
        "",
        f"Generated: {generated_at}  ",
        f"Window checked: {since_date} to {until_date}  ",
        f"Calls analyzed: **{len(run_result.store.calls)}**  ",
        f"Cadence status: {_cadence_line(cadence)}",
        "",
    ]

    if not run_result.finalized:
        lines.append(
            f"> **Note:** the agent ended without calling `finalize_report` "
            f"(used {run_result.turn_count} turn(s), last stop_reason: `{run_result.final_stop_reason}`). "
            "Any scorecard item below with no recorded finding is marked as a run limitation, not a "
            "genuine ROE gap - re-run or raise the turn cap if this happens often."
        )
        lines.append("")

    lines.append("## Scorecard")
    lines.append("")
    findings = run_result.scorecard.findings
    for item_id, description in ROE_ITEMS.items():
        lines.append(f"### {description}")
        finding = findings.get(item_id)
        if finding is None:
            status = "unverifiable" if run_result.finalized else "not_evaluated"
            lines.append(f"**{STATUS_LABEL[status]}**")
        else:
            lines.append(f"**{STATUS_LABEL[finding['status']]}**")
            if finding.get("evidence_quote"):
                lines.append(f"> {finding['evidence_quote']}")
                lines.append(f"> — call on {finding.get('call_date', 'unknown date')}")
        lines.append("")

    lines.append("## Follow-up Commitments")
    lines.append("")
    if not run_result.scorecard.commitments:
        lines.append("None recorded.")
    else:
        lines.append("| Commitment | Made by | Made on | Status | Confirmed on |")
        lines.append("|---|---|---|---|---|")
        for c in run_result.scorecard.commitments:
            lines.append(
                f"| {c['description']} | {c['made_by']} | {c['made_on_call_date']} | "
                f"{c['status']} | {c.get('confirmed_on_call_date') or '-'} |"
            )
    lines.append("")

    lines.append(_not_assessed_section())
    return "\n".join(lines)
