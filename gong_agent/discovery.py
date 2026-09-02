"""Auto-discover which accounts are currently in a POC, instead of hand-
maintaining a domain list per account.

Groups calls by the Salesforce Account attached via Gong's CRM context,
filtered to calls whose Opportunity is in the "Evaluation POC" stage -
confirmed against Komodor's actual Salesforce pipeline (a fixed set of
stage names: Pipeline, Qualified, Validate, Evaluation POC, Negotiation,
Closed Won, Closed Lost). Optionally scoped to one SE's calls by checking
whether their email appears as an internal party - no server-side
"primaryUserId" filter was found to work reliably for this (see research),
so this is done client-side against the parties data already returned in
the same request as the CRM context (no extra API cost).

No manual per-account setup needed. What's left as a hand-maintained
choice is only cadence_overrides.yaml (gong_agent/config.py), for the rare
account whose agreed cadence isn't the default.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from gong_agent.gong_client import CallRecord, GongClient

DEFAULT_LOOKBACK_DAYS = 90
POC_STAGE_NAME = "Evaluation POC"


@dataclass
class DiscoveredAccount:
    account_name: str
    crm_account_id: str | None
    calls: list[CallRecord] = field(default_factory=list)


def discovery_window(lookback_days: int = DEFAULT_LOOKBACK_DAYS) -> tuple[str, str]:
    """Plain YYYY-MM-DD (since_date, until_date) for the fixed lookback
    window - every discovered account uses this same window; there is no
    per-account "POC start date" available from Gong to do better than a
    fixed lookback (see plan discussion)."""
    until = datetime.now(timezone.utc).date()
    since = until - timedelta(days=lookback_days)
    return since.isoformat(), until.isoformat()


def discover_poc_accounts(
    client: GongClient,
    since_date: str,
    until_date: str,
    poc_stage: str = POC_STAGE_NAME,
    se_email: str | None = None,
) -> list[DiscoveredAccount]:
    """Manager mode (se_email=None): every account currently in `poc_stage`.
    IC mode (se_email set): only accounts where that email appears as an
    internal party on at least one of the matched calls."""
    all_calls = client.list_calls(f"{since_date}T00:00:00Z", f"{until_date}T23:59:59Z")

    poc_stage_lower = poc_stage.strip().lower()
    matched = [c for c in all_calls if (c.opportunity_stage or "").strip().lower() == poc_stage_lower]

    if se_email:
        se_email_lower = se_email.strip().lower()
        matched = [
            c for c in matched
            if any((p.email or "").lower() == se_email_lower for p in c.parties)
        ]

    groups: dict[str, DiscoveredAccount] = {}
    for call in matched:
        if not call.crm_account_name:
            continue  # can't group without an account identifier
        # Keyed by name, not crm_account_id: Gong's CRM context doesn't
        # reliably attach an account id to every call for the same account,
        # and an id-preferring key used to split one account into two
        # groups (and two identically-named report artifacts) whenever some
        # of its calls had the id and others didn't.
        key = call.crm_account_name.strip().lower()
        if key not in groups:
            groups[key] = DiscoveredAccount(account_name=call.crm_account_name, crm_account_id=call.crm_account_id)
        elif not groups[key].crm_account_id and call.crm_account_id:
            groups[key].crm_account_id = call.crm_account_id
        groups[key].calls.append(call)

    return sorted(groups.values(), key=lambda a: a.account_name)
