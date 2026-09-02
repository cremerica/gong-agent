"""Cadence math (pure code, no LLM) and the agent's working-memory state:
the scorecard the model builds up via record_finding, and the follow-up
commitment list via record_commitment. finalize_report flips `finalized`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from gong_agent.gong_client import CallRecord

ROE_ITEMS = {
    "success_criteria_agreed": "Success criteria explicitly agreed and re-confirmed by name at close (ROE 1.1, 3.1, 5.1)",
    "kickoff_training_delivered": "Kickoff/training actually delivered - walkthrough, attendees, champions named (ROE 3.1)",
    "sync_cadence_maintained": "Sync-call cadence held roughly at the agreed frequency, no unexplained gap (ROE 3.1)",
    "value_demonstrated_on_syncs": "Value demonstrated on sync calls - worked examples, quantified before/after, Rigging-report-style evidence (ROE 3.6)",
    "sentiment_and_product_feedback": "Sentiment (esp. neutral) and product feedback surfaced and noted (ROE 5.2)",
    "closing_discipline": "Commercial-recommendation question asked; if not an unconditional yes, correct in-the-moment discovery prompt used (ROE 5.3, 5.4, 5.5)",
}

FINDING_STATUSES = {"evidence_found", "gap", "unverifiable"}
COMMITMENT_MADE_BY = {"se", "prospect"}
COMMITMENT_STATUSES = {"open", "confirmed_done"}


def compute_cadence_gap(calls: list[CallRecord], expected_cadence_days: int, today: date | None = None) -> dict:
    today = today or datetime.now(timezone.utc).date()
    if not calls:
        return {
            "status": "no_data",
            "last_call_date": None,
            "days_since_last_call": None,
            "expected_cadence_days": expected_cadence_days,
            "historical_gaps": [],
        }

    dates = sorted({datetime.strptime(c.date, "%Y-%m-%d").date() for c in calls if c.date})
    last_call = dates[-1]
    days_since_last = (today - last_call).days

    # A gap of >1.5x the expected cadence is flagged as overdue right now.
    current_status = "gap" if days_since_last > expected_cadence_days * 1.5 else "on_track"

    # Also flag any historical gap between consecutive calls that blew past
    # 2x the expected cadence - "cadence held" means the whole window, not
    # just whether a call happened recently.
    historical_gaps = []
    for prev, nxt in zip(dates, dates[1:]):
        gap_days = (nxt - prev).days
        if gap_days > expected_cadence_days * 2:
            historical_gaps.append({"from": prev.isoformat(), "to": nxt.isoformat(), "days": gap_days})

    return {
        "status": current_status,
        "last_call_date": last_call.isoformat(),
        "days_since_last_call": days_since_last,
        "expected_cadence_days": expected_cadence_days,
        "historical_gaps": historical_gaps,
    }


@dataclass
class ScorecardState:
    findings: dict[str, dict] = field(default_factory=dict)
    commitments: list[dict] = field(default_factory=list)
    finalized: bool = False

    def record_finding(self, roe_item: str, status: str, evidence_quote: str | None,
                        call_date: str | None, call_id: str | None) -> dict:
        if roe_item not in ROE_ITEMS:
            raise ValueError(f"unknown roe_item '{roe_item}'. Valid values: {sorted(ROE_ITEMS)}")
        if status not in FINDING_STATUSES:
            raise ValueError(f"unknown status '{status}'. Valid values: {sorted(FINDING_STATUSES)}")
        if status == "evidence_found" and not (evidence_quote and call_date):
            raise ValueError("status='evidence_found' requires both evidence_quote and call_date")

        self.findings[roe_item] = {
            "status": status,
            "evidence_quote": evidence_quote,
            "call_date": call_date,
            "call_id": call_id,
        }
        return self.snapshot_findings()

    def record_commitment(self, description: str, made_on_call_date: str, made_by: str,
                           status: str, confirmed_on_call_date: str | None = None) -> list[dict]:
        if made_by not in COMMITMENT_MADE_BY:
            raise ValueError(f"made_by must be one of {sorted(COMMITMENT_MADE_BY)}")
        if status not in COMMITMENT_STATUSES:
            raise ValueError(f"status must be one of {sorted(COMMITMENT_STATUSES)}")
        if status == "confirmed_done" and not confirmed_on_call_date:
            raise ValueError("status='confirmed_done' requires confirmed_on_call_date")

        self.commitments.append({
            "description": description,
            "made_on_call_date": made_on_call_date,
            "made_by": made_by,
            "status": status,
            "confirmed_on_call_date": confirmed_on_call_date,
        })
        return self.commitments

    def snapshot_findings(self) -> dict:
        return dict(self.findings)
