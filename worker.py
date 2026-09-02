#!/usr/bin/env python3
"""AgentOps worker entrypoint: discovers accounts currently in POC and runs
the health agent for each, uploading a Markdown report per account as a run
artifact and returning a structured summary + rolled-up text digest.

Supersedes main.py (retired) - same discovery + per-account loop as before,
now wired to the komodor-agentops SDK instead of argparse + local files.
gong_agent/ (discovery, Gong API access, the agent loop, report assembly)
is unchanged by this migration - see agent-spec.yaml for the agent's
identity/input schema/trigger and the README for the deploy story.

NOTE: `observe` and `apply_secret_to_env` are imported from the top-level
`komodor_agentops` package to mirror AgentOpsWorker/AgentSpec's import style
in the SDK docs, which don't give an explicit submodule path for either -
verify against the installed SDK if this import fails.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from komodor_agentops import AgentOpsWorker, AgentSpec, apply_secret_to_env, get_secret, observe
from komodor_agentops.worker import Run

from gong_agent.account_data import AccountDataStore
from gong_agent.agent_loop import run_agent
from gong_agent.config import AccountConfig, expected_cadence_for, load_cadence_overrides
from gong_agent.discovery import DEFAULT_LOOKBACK_DAYS, DiscoveredAccount, discover_poc_accounts, discovery_window
from gong_agent.gong_client import GongClient
from gong_agent.report import build_report, build_zero_calls_report

AGENT_SPEC = AgentSpec.from_dir(
    Path(__file__).parent,
    agent_card={"capabilities": {"chat": True, "ask": True, "streaming": True}},
)


@observe(name="process_account", as_type="tool")
def _process_account(discovered: DiscoveredAccount, gong_client: GongClient, cadence_overrides: dict[str, int],
                      since_date: str, until_date: str) -> tuple[AccountConfig, str, dict[str, Any]]:
    """Mirrors main.py's retired run_one_account, minus the disk write -
    returns the account identity, the report Markdown, and a small
    structured summary for the run's output."""
    account = AccountConfig(
        account_name=discovered.account_name,
        crm_account_id=discovered.crm_account_id,
        expected_cadence_days=expected_cadence_for(discovered.account_name, cadence_overrides),
    )

    store = AccountDataStore(client=gong_client, account=account)
    store.seed_calls(discovered.calls)

    if not store.calls:
        report_md = build_zero_calls_report(account, since_date, until_date)
        summary = {"account": account.account_name, "calls_analyzed": 0, "cadence_status": "no_data"}
        return account, report_md, summary

    result = run_agent(store, since_date, until_date)
    report_md = build_report(account, result, since_date, until_date)
    summary = {
        "account": account.account_name,
        "calls_analyzed": len(store.calls),
        "finalized": result.finalized,
        "turns": result.turn_count,
        "stop_reason": result.final_stop_reason,
    }
    return account, report_md, summary


async def handler(run: Run) -> dict[str, Any]:
    # Per-run bound credentials - see "Secrets and credentials" in the docs.
    # The Gong credentials are bound in AgentOps under "gong-username" /
    # "gong-password" (shared names already used by another agent - AgentOps
    # doesn't allow the same credential under two names), so these are
    # fetched by name rather than via apply_secret_to_env, which assumes the
    # credential name and the env var it lands in are the same string.
    apply_secret_to_env("ANTHROPIC_API_KEY")
    gong_access_key = get_secret("gong-username") or os.environ["GONG_ACCESS_KEY"]
    gong_access_key_secret = get_secret("gong-password") or os.environ["GONG_ACCESS_KEY_SECRET"]

    account_filter = run.input.get("account")
    se_email = run.input.get("se_email") or os.environ.get("SE_EMAIL")
    since_date, until_date = discovery_window(DEFAULT_LOOKBACK_DAYS)

    gong_client = GongClient(
        access_key=gong_access_key,
        access_key_secret=gong_access_key_secret,
    )

    mode = f"IC mode ({se_email})" if se_email else "manager mode (all accounts in POC)"
    print(f"Discovering accounts - {mode}, window {since_date} to {until_date}...", file=sys.stderr)

    discovered = discover_poc_accounts(gong_client, since_date, until_date, se_email=se_email)
    if account_filter:
        discovered = [a for a in discovered if a.account_name.lower() == account_filter.lower()]

    cadence_overrides = load_cadence_overrides()
    today = datetime.now(timezone.utc).date().isoformat()

    summaries: list[dict[str, Any]] = []
    digest_sections: list[str] = []

    for d in discovered:
        try:
            account, report_md, summary = _process_account(d, gong_client, cadence_overrides, since_date, until_date)
            await run.upload_artifact(name=f"{account.slug}/{today}.md", content=report_md)
            summaries.append(summary)
            digest_sections.append(
                f"## {account.account_name}\n\n"
                f"Calls analyzed: {summary.get('calls_analyzed', 0)}"
                + (f" | finalized: {summary['finalized']}" if "finalized" in summary else "")
            )
        # Isolate one account's failure from the rest of this aggregate run -
        # main.py let any account's error kill the whole (disposable) CI job,
        # but that would now sink every other account's report too.
        except Exception as e:  # noqa: BLE001
            print(f"[{d.account_name}] failed: {e}", file=sys.stderr)
            summaries.append({"account": d.account_name, "error": str(e)})
            digest_sections.append(f"## {d.account_name}\n\nFailed: {e}")

    text = (
        f"# POC Health - {mode}\n\nWindow: {since_date} to {until_date}\n\n"
        + ("\n\n".join(digest_sections) if digest_sections else "No accounts currently in POC stage.")
    )

    return {
        "accounts": summaries,
        "accounts_processed": len(discovered),
        "text": text,
    }


def main() -> None:
    AgentOpsWorker(agent=AGENT_SPEC, on_run=handler).run()


if __name__ == "__main__":
    main()
