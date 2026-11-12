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

QUERIES = ("license plate reader OR ALPR",)


class LegiScanCollector:
    def collect(self, source: dict, *, since: date, until: date, http: HttpClient) -> list[RawDocument]:
        key = os.environ.get("LEGISCAN_API_KEY")
        if not key:
            raise MissingKeyError("LEGISCAN_API_KEY")
        state = source["state"]
        seen: set[int] = set()
        documents: list[RawDocument] = []
        queries = (source["query"],) if source.get("query") else QUERIES
        for query in queries:
            search_url = (
                "https://api.legiscan.com/?key="
                + quote(key)
                + f"&op=getSearch&state={state}&query={quote(query)}&year=2"
            )
            data = http.get(search_url).json()
            if data.get("status") != "OK":
                continue
            results = data.get("searchresult") or {}
            if isinstance(results, list):
                items = [item for item in results if isinstance(item, dict)]
            else:
                items = [item for key_name, item in results.items() if key_name != "summary" and isinstance(item, dict)]
            for item in items:
                bill_id = item.get("bill_id")
                if not bill_id or bill_id in seen:
                    continue
                seen.add(bill_id)
                detail = http.get(
                    f"https://api.legiscan.com/?key={key}&op=getBill&id={bill_id}"
                ).json()
                bill = (detail.get("bill") or {}) if detail.get("status") == "OK" else {}
                text = _bill_text(bill, item)
                last_action = item.get("last_action")
                last_action_date = last_action.get("date") if isinstance(last_action, dict) else None
                change = item.get("last_action_date") or item.get("status_date") or last_action_date or ""
                published = str(change)[:10] if change else None
                # year=2 is already the current session. Do not drop a match
                # because its last action is older than this run's window.
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
