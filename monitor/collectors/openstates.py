"""Open States API v3.

Root https://v3.openstates.org/. Send the key in the X-API-KEY header.
https://docs.openstates.org/api-v3/ checked 2026-11-12.
This collector pulls recent events (hearings). People and committees are
exported by scripts/seed_registry.py and are not written into config/.
"""

from __future__ import annotations

import os
from datetime import date

from monitor.collectors.base import MissingKeyError, RawDocument
from monitor.http import HttpClient

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
        payload = http.get(url, headers={"X-API-KEY": key, "Accept": "application/json"}).json()
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
        return documents
