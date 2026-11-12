"""LegiScan public pull API.

GET https://api.legiscan.com/?key=KEY&op=getSearch&state=GA&query=QUERY&year=2
then op=getBill&id=BILL_ID. Public tier is 10,000 queries per month.
https://legiscan.com/legiscan and the API user manual, checked 2026-11-12.
"""

from __future__ import annotations

import os
from datetime import date
from urllib.parse import quote

from monitor.collectors.base import MissingKeyError, RawDocument
from monitor.http import HttpClient

QUERIES = ("\"license plate\"", "ALPR", "\"Flock Safety\"")


class LegiScanCollector:
    def collect(self, source: dict, *, since: date, until: date, http: HttpClient) -> list[RawDocument]:
        key = os.environ.get("LEGISCAN_API_KEY")
        if not key:
            raise MissingKeyError("LEGISCAN_API_KEY")
        state = source["state"]
        seen: set[int] = set()
        documents: list[RawDocument] = []
        for query in QUERIES:
            search_url = (
                "https://api.legiscan.com/?key="
                + quote(key)
                + f"&op=getSearch&state={state}&query={quote(query)}&year=2"
            )
            data = http.get(search_url).json()
            if data.get("status") != "OK":
                continue
            results = data.get("searchresult") or {}
            for item_key, item in results.items():
                if item_key == "summary" or not isinstance(item, dict):
                    continue
                bill_id = item.get("bill_id")
                if not bill_id or bill_id in seen:
                    continue
                seen.add(bill_id)
                detail = http.get(
                    f"https://api.legiscan.com/?key={key}&op=getBill&id={bill_id}"
                ).json()
                bill = (detail.get("bill") or {}) if detail.get("status") == "OK" else {}
                text = _bill_text(bill, item)
                change = item.get("last_action_date") or item.get("status_date") or ""
                published = change[:10] if change else None
                if published:
                    try:
                        stamp = date.fromisoformat(published)
                    except ValueError:
                        stamp = None
                    if stamp and (stamp < since or stamp > until):
                        # Still keep bills whose last action is recent; LegiScan
                        # dates are the action date. Drop clearly stale ones.
                        continue
                url_out = item.get("url") or bill.get("url") or f"https://legiscan.com/{state}/bill/{item.get('bill_number')}"
                documents.append(
                    RawDocument(
                        source_id=source["id"],
                        url=url_out,
                        title=f"{item.get('bill_number') or ''} {item.get('title') or bill.get('title') or ''}".strip(),
                        published_at=published,
                        doc_type="bill",
                        text=text,
                    )
                )
                if len(documents) >= int(source.get("max_docs") or 15):
                    return documents
        return documents


def _bill_text(bill: dict, item: dict) -> str:
    sponsors = []
    for sponsor in bill.get("sponsors") or []:
        name = sponsor.get("name") or ""
        if name:
            sponsors.append(name)
    history = []
    for action in (bill.get("history") or [])[-8:]:
        history.append(f"{action.get('date') or ''} {action.get('action') or ''}".strip())
    parts = [
        item.get("bill_number") or bill.get("bill_number") or "",
        item.get("title") or bill.get("title") or "",
        item.get("description") or bill.get("description") or "",
        "Sponsors: " + ", ".join(sponsors) if sponsors else "",
        "\n".join(history),
    ]
    return "\n".join(part for part in parts if part)
