"""Account identity (from discovery) and the small optional cadence-overrides
config - accounts themselves are no longer hand-configured (see
gong_agent/discovery.py); this file only covers the one thing that still
needs occasional manual input: an account's expected sync-call cadence.
"""

from __future__ import annotations

from dataclasses import dataclass

import yaml

DEFAULT_CADENCE_DAYS = 7


@dataclass
class AccountConfig:
    """Identity for one discovered account, used by the agent loop/report -
    not hand-written config. See discovery.DiscoveredAccount."""

    account_name: str
    crm_account_id: str | None
    expected_cadence_days: int

    @property
    def slug(self) -> str:
        return "".join(c.lower() if c.isalnum() else "-" for c in self.account_name).strip("-")


def load_cadence_overrides(path: str = "cadence_overrides.yaml") -> dict[str, int]:
    """Optional file: {account_name: expected_cadence_days}, only needed for
    accounts whose agreed cadence differs from DEFAULT_CADENCE_DAYS. Missing
    file is not an error - just means every account uses the default."""
    try:
        with open(path) as f:
            raw = yaml.safe_load(f) or {}
    except FileNotFoundError:
        return {}
    return {str(name).lower(): int(days) for name, days in raw.items()}


def expected_cadence_for(account_name: str, overrides: dict[str, int]) -> int:
    return overrides.get(account_name.lower(), DEFAULT_CADENCE_DAYS)
