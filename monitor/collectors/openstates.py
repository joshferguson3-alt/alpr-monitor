"""Open States API v3.

Root https://v3.openstates.org/. Send the key in the X-API-KEY header.
https://docs.openstates.org/api-v3/ checked 2026-11-12.
This collector pulls recent events (hearings). People and committees are
exported by scripts/seed_registry.py and are not written into config/.
"""

from __future__ import annotations

import os
from datetime import date

from urllib.parse import quote

from monitor.collectors.base import MissingKeyError, RawDocument
from monitor.constants import BILL_QUERY
from monitor.http import HttpClient

def _openstates_get(http: HttpClient, url: str, key: str) -> dict:
    """Space calls out. A 429 is retried inside the HTTP client."""
    http.pause(3)
    payload = http.get(url, headers={"X-API-KEY": key, "Accept": "application/json"}, retries=4).json()
    return payload if isinstance(payload, dict) else {}


JURISDICTION = {
    "AL": "Alabama",
    "FL": "Florida",
    "GA": "Georgia",
    "KY": "Kentucky",
    "LA": "Louisiana",
    "MS": "Mississippi",
    "NC": "North Carolina",
    "SC": "South Carolina",
    "TN": "Tennessee",
    "VA": "Virginia",
}


class OpenStatesCollector:
    def collect(self, source: dict, *, since: date, until: date, http: HttpClient) -> list[RawDocument]:
        key = os.environ.get("OPENSTATES_API_KEY")
        if not key:
            raise MissingKeyError("OPENSTATES_API_KEY")
        state = source.get("state") or "GA"
        jurisdiction = JURISDICTION.get(state, state)
        url = (
            "https://v3.openstates.org/events"
            f"?jurisdiction={jurisdiction}&per_page=20"
        )
        payload = _openstates_get(http, url, key)
        results = payload.get("results") or payload.get("data") or []
        documents: list[RawDocument] = []
        for event in results:
            start = (event.get("start_date") or "")[:10]
            if start:
                try:
                    stamp = date.fromisoformat(start)
                except ValueError:
                    stamp = None
                if stamp and (stamp < since or stamp > until):
                    continue
            name = event.get("name") or "Hearing"
            description = event.get("description") or ""
            agenda = []
            for item in event.get("agenda") or []:
                agenda.append(item.get("description") or item.get("note") or "")
            text = "\n".join([name, description, *agenda])
            event_id = event.get("id") or name
            link = event.get("sources") or []
            url_out = link[0].get("url") if link and isinstance(link[0], dict) and link[0].get("url") else f"https://openstates.org/event/{event_id}"
            documents.append(
                RawDocument(
                    source_id=source["id"],
                    url=url_out,
                    title=name,
                    published_at=start or None,
                    doc_type=source.get("doc_type") or "bill",
                    text=text,
                )
            )
        documents.extend(self._bills(source, jurisdiction, http, key))
        return documents

    def _bills(self, source: dict, jurisdiction: str, http: HttpClient, key: str) -> list[RawDocument]:
        """Bill search backup. Search hits are kept even when the last action is old."""
        query = source.get("query") or BILL_QUERY
        documents: list[RawDocument] = []
        seen: set[str] = set()
        for page in range(1, 4):
            url = (
                "https://v3.openstates.org/bills?jurisdiction="
                + quote(jurisdiction)
                + "&q="
                + quote(query)
                + f"&per_page=20&page={page}&sort=updated_desc&include=sponsorships"
            )
            payload = _openstates_get(http, url, key)
            results = payload.get("results") or []
            if not results:
                break
            for bill in results:
                identifier = bill.get("identifier") or bill.get("id") or ""
                if not identifier or identifier in seen:
                    continue
                seen.add(identifier)
                abstracts = []
                for abstract in bill.get("abstracts") or []:
                    if isinstance(abstract, dict):
                        abstracts.append(abstract.get("abstract") or "")
                    elif isinstance(abstract, str):
                        abstracts.append(abstract)
                sponsors = []
                for sponsor in bill.get("sponsorships") or []:
                    if isinstance(sponsor, dict) and sponsor.get("name"):
                        sponsors.append(sponsor["name"])
                action = ((bill.get("latest_action") or {}).get("description") or "") if isinstance(bill.get("latest_action"), dict) else ""
                action_date = ""
                if isinstance(bill.get("latest_action"), dict):
                    action_date = (bill["latest_action"].get("date") or "")[:10]
                text = "\n".join(
                    part
                    for part in (
                        identifier,
                        bill.get("title") or "",
                        "\n".join(abstracts),
                        action,
                        "Sponsors: " + ", ".join(sponsors) if sponsors else "",
                    )
                    if part
                )
                link = bill.get("openstates_url") or ""
                if not link:
                    sources = bill.get("sources") or []
                    if sources and isinstance(sources[0], dict):
                        link = sources[0].get("url") or ""
                if not link:
                    link = f"https://openstates.org/bills/{identifier}"
                documents.append(
                    RawDocument(
                        source_id=source["id"],
                        url=link,
                        title=f"{identifier} {bill.get('title') or ''}".strip(),
                        published_at=action_date or None,
                        doc_type="bill",
                        text=text,
                        force_prefilter=True,
                    )
                )
                if len(documents) >= int(source.get("max_docs") or 20):
                    return documents
            pagination = payload.get("pagination") or {}
            if page >= int(pagination.get("max_page") or page):
                break
        return documents
