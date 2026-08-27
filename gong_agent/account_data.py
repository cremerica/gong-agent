"""Per-run account data layer: CRM-account matching, transcript formatting,
and in-process caching so the agent's tool calls (get_transcript, search_calls)
never re-fetch the same call twice in a run.

Account matching uses the Salesforce Account attached via Gong's CRM
context (see discovery.py) rather than participant email domains - more
accurate and needs no per-account config. Gong still has no transcript-
search endpoint, so search() remains a local scan.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from gong_agent.config import AccountConfig
from gong_agent.gong_client import CallRecord, GongClient


@dataclass
class FormattedCall:
    call_id: str
    date: str
    text: str  # "[Speaker Name] (00:12:03): ..." lines, one per monologue


def _format_seconds(ms: int) -> str:
    total_seconds = ms // 1000
    h, rem = divmod(total_seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


@dataclass
class AccountDataStore:
    """Holds this run's fetched calls/transcripts so tools.py can be a thin
    dispatcher without threading Gong state through every tool call."""

    client: GongClient
    account: AccountConfig
    calls: list[CallRecord] = field(default_factory=list)
    _transcript_cache: dict[str, FormattedCall] = field(default_factory=dict)

    def matches_account(self, call: CallRecord) -> bool:
        if self.account.crm_account_id:
            return call.crm_account_id == self.account.crm_account_id
        return bool(call.crm_account_name) and call.crm_account_name.lower() == self.account.account_name.lower()

    def seed_calls(self, calls: list[CallRecord]) -> None:
        """Use calls already fetched during discovery (discovery.py) instead
        of re-querying Gong - avoids one org-wide fetch per account."""
        self.calls = calls

    def load_calls(self, since_date: str, until_date: str) -> list[CallRecord]:
        """Fetch calls in the window and filter to this account by its CRM
        Account match. Widening/narrowing the window (the model can call
        list_calls again with different dates) simply re-runs this and
        replaces self.calls."""
        all_calls = self.client.list_calls(f"{since_date}T00:00:00Z", f"{until_date}T23:59:59Z")
        self.calls = [c for c in all_calls if self.matches_account(c)]
        return self.calls

    def _speaker_name(self, call: CallRecord, speaker_id: str) -> str:
        for p in call.parties:
            if p.speaker_id == speaker_id:
                if p.name:
                    return p.name
                if p.email:
                    return p.email
        return f"Unknown speaker ({speaker_id})"

    def get_transcript(self, call_id: str) -> FormattedCall | None:
        if call_id in self._transcript_cache:
            return self._transcript_cache[call_id]

        call = next((c for c in self.calls if c.call_id == call_id), None)
        if call is None:
            return None

        monologues = self.client.get_transcript_raw(call_id)
        lines = []
        for mono in monologues:
            speaker_id = mono.get("speakerId", "")
            name = self._speaker_name(call, speaker_id)
            for sentence in mono.get("sentences", []):
                ts = _format_seconds(sentence.get("start", 0))
                lines.append(f"[{name}] ({ts}): {sentence.get('text', '')}")

        formatted = FormattedCall(call_id=call_id, date=call.date, text="\n".join(lines))
        self._transcript_cache[call_id] = formatted
        return formatted

    def search(self, query: str) -> list[dict]:
        """Local case-insensitive substring scan across all of this account's
        calls (fetching+caching any not yet pulled), with a few lines of
        surrounding context per hit. Gong has no server-side transcript
        search - see plan doc."""
        query_lower = query.lower()
        results = []
        for call in self.calls:
            formatted = self.get_transcript(call.call_id)
            if formatted is None:
                continue
            lines = formatted.text.split("\n")
            for i, line in enumerate(lines):
                if query_lower in line.lower():
                    start = max(0, i - 2)
                    end = min(len(lines), i + 3)
                    snippet = "\n".join(lines[start:end])
                    results.append({"call_id": call.call_id, "call_date": call.date, "snippet": snippet})
        return results
