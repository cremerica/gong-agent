"""Thin Gong API client.

Covers exactly what this agent needs: list calls in a date window (with
parties AND Salesforce CRM context joined in), fetch transcripts, and join
speaker names. Gong has no native "list calls for CRM account X" endpoint
and no transcript-search endpoint - both are worked around here / in
tools.py rather than assumed to exist server-side.

Account discovery (gong_agent/discovery.py) groups calls by the Salesforce
Account attached via context, filtered to calls whose Opportunity is in the
"Evaluation POC" stage - confirmed live against Komodor's Gong tenant, which
does have an active Salesforce integration even though individual SEs may
lack personal Salesforce API access (a tenant-level setting, not a personal
credential).
"""

from __future__ import annotations

import base64
import time
from dataclasses import dataclass, field

import requests

BASE_URL = "https://api.gong.io"
MAX_PAGE_SIZE = 100


class GongAPIError(RuntimeError):
    pass


@dataclass
class Party:
    speaker_id: str
    name: str | None
    email: str | None
    affiliation: str | None  # "Internal" | "External" | "Unknown" per Gong


@dataclass
class CallRecord:
    call_id: str
    started: str  # ISO 8601
    date: str  # YYYY-MM-DD, derived from started
    title: str | None
    duration_seconds: int | None
    parties: list[Party] = field(default_factory=list)
    # Salesforce context, if this Gong tenant's CRM integration attached one.
    crm_account_name: str | None = None
    crm_account_id: str | None = None
    opportunity_name: str | None = None
    opportunity_stage: str | None = None


class GongClient:
    def __init__(self, access_key: str, access_key_secret: str, session: requests.Session | None = None):
        token = base64.b64encode(f"{access_key}:{access_key_secret}".encode()).decode()
        self._headers = {
            "Authorization": f"Basic {token}",
            "Content-Type": "application/json",
        }
        self._session = session or requests.Session()

    def _request(self, method: str, path: str, **kwargs) -> dict:
        url = f"{BASE_URL}{path}"
        for attempt in range(5):
            resp = self._session.request(method, url, headers=self._headers, timeout=30, **kwargs)
            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", "2"))
                time.sleep(retry_after)
                continue
            if resp.status_code >= 500:
                time.sleep(2**attempt)
                continue
            if not resp.ok:
                raise GongAPIError(f"{method} {path} -> {resp.status_code}: {resp.text[:500]}")
            return resp.json() if resp.text else {}
        raise GongAPIError(f"{method} {path} failed after retries")

    @staticmethod
    def _parse_call_entry(call: dict) -> CallRecord | None:
        meta = call.get("metaData", call)
        call_id = meta.get("id") or call.get("id")
        if not call_id:
            return None
        started = meta.get("started") or meta.get("actualStart") or ""

        parties = [
            Party(
                speaker_id=p.get("speakerId") or "",
                name=p.get("name"),
                email=p.get("emailAddress"),
                affiliation=p.get("affiliation"),
            )
            for p in call.get("parties", [])
        ]

        crm_account_name = crm_account_id = opportunity_name = opportunity_stage = None
        for system_ctx in call.get("context", []):
            if system_ctx.get("system") != "Salesforce":
                continue
            for obj in system_ctx.get("objects", []):
                fields = {f["name"]: f["value"] for f in obj.get("fields", [])}
                if obj.get("objectType") == "Account":
                    crm_account_name = fields.get("Name")
                    crm_account_id = obj.get("objectId")
                elif obj.get("objectType") == "Opportunity":
                    opportunity_name = fields.get("Name")
                    opportunity_stage = fields.get("StageName")

        return CallRecord(
            call_id=call_id,
            started=started,
            date=started[:10] if started else "",
            title=meta.get("title"),
            duration_seconds=meta.get("duration"),
            parties=parties,
            crm_account_name=crm_account_name,
            crm_account_id=crm_account_id,
            opportunity_name=opportunity_name,
            opportunity_stage=opportunity_stage,
        )

    def list_calls(self, from_date_iso: str, to_date_iso: str) -> list[CallRecord]:
        """List calls in a window with parties AND Salesforce CRM context
        joined in, in one pass (both come back in the same response - no
        second round trip needed).

        Uses POST /v2/calls/extensive with a date-range filter (not the
        plain POST /v2/calls, which is Gong's "add a manually-recorded call"
        endpoint, not a listing endpoint - confirmed by a live 400 requiring
        clientUniqueId/parties/actualStart/direction when this was tried).
        """
        records: list[CallRecord] = []
        cursor: str | None = None
        while True:
            body = {
                "filter": {
                    "fromDateTime": from_date_iso,
                    "toDateTime": to_date_iso,
                },
                "contentSelector": {"exposedFields": {"parties": True}, "context": "Extended"},
            }
            if cursor:
                body["cursor"] = cursor
            data = self._request("POST", "/v2/calls/extensive", json=body)
            for call in data.get("calls", []):
                record = self._parse_call_entry(call)
                if record is not None:
                    records.append(record)
            cursor = (data.get("records") or {}).get("cursor")
            if not cursor:
                break
        return records

    def get_parties(self, call_ids: list[str]) -> dict[str, list[Party]]:
        """POST /v2/calls/extensive by explicit call_ids - returns {call_id: [Party, ...]}.
        Used when re-fetching parties for specific calls outside a date-range listing."""
        if not call_ids:
            return {}
        result: dict[str, list[Party]] = {}
        for i in range(0, len(call_ids), MAX_PAGE_SIZE):
            chunk = call_ids[i : i + MAX_PAGE_SIZE]
            body = {
                "filter": {"callIds": chunk},
                "contentSelector": {"exposedFields": {"parties": True}},
            }
            data = self._request("POST", "/v2/calls/extensive", json=body)
            for call in data.get("calls", []):
                record = self._parse_call_entry(call)
                if record is not None:
                    result[record.call_id] = record.parties
        return result

    def get_transcript_raw(self, call_id: str) -> list[dict]:
        """POST /v2/calls/transcript for a single call. Returns the list of
        monologues: [{speakerId, topic, sentences: [{start, end, text}]}]."""
        body = {"filter": {"callIds": [call_id]}}
        data = self._request("POST", "/v2/calls/transcript", json=body)
        transcripts = data.get("callTranscripts", [])
        if not transcripts:
            return []
        return transcripts[0].get("transcript", [])
