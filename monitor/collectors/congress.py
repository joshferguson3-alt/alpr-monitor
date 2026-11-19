"""Congress.gov API v3.

GET https://api.congress.gov/v3/bill?query=...&api_key=KEY
The query parameter was confirmed against the live API on 2026-11-12.
"""

from __future__ import annotations

import os
from datetime import date
from urllib.parse import quote

from monitor.collectors.base import MissingKeyError, RawDocument
from monitor.constants import BILL_QUERY
from monitor.http import HttpClient


class CongressCollector:
    def collect(self, source: dict, *, since: date, until: date, http: HttpClient) -> list[RawDocument]:
        key = os.environ.get("CONGRESS_API_KEY")
        if not key:
            raise MissingKeyError("CONGRESS_API_KEY")
        query = source.get("query") or BILL_QUERY
        url = (
            "https://api.congress.gov/v3/bill?query="
            + quote(query)
            + "&limit=20&api_key="
            + quote(key)
            + "&sort=updateDate+desc"
        )
        payload = http.get(url, headers={"Accept": "application/json"}).json()
        documents: list[RawDocument] = []
        for bill in payload.get("bills") or []:
            introduced = (bill.get("introducedDate") or bill.get("updateDate") or "")[:10]
            if introduced:
                try:
                    stamp = date.fromisoformat(introduced)
                except ValueError:
                    stamp = None
                # Congress search is not date-filtered. Keep items updated inside the window
                # when updateDate is present; otherwise keep the search hit and let the prefilter decide.
                updated = (bill.get("updateDate") or "")[:10]
                if updated:
                    try:
                        updated_on = date.fromisoformat(updated)
                    except ValueError:
                        updated_on = None
                    if updated_on and updated_on < since:
                        continue
            number = bill.get("number")
            bill_type = (bill.get("type") or "").lower()
            congress = bill.get("congress")
            public = _public_url(congress, bill_type, number)
            action = (bill.get("latestAction") or {}).get("text") or ""
            title = bill.get("title") or "Bill"
            text = f"{bill_type.upper()} {number}\n{title}\n{action}"
            documents.append(
                RawDocument(
                    source_id=source["id"],
                    url=public,
                    title=f"{bill_type.upper()} {number}: {title}"[:300],
                    published_at=introduced or None,
                    doc_type="bill",
                    text=text,
                )
            )
        return documents


def _public_url(congress, bill_type: str, number) -> str:
    ordinal = f"{congress}th" if congress else ""
    slug = {
        "hr": "house-bill",
        "s": "senate-bill",
        "hjres": "house-joint-resolution",
        "sjres": "senate-joint-resolution",
        "hconres": "house-concurrent-resolution",
        "sconres": "senate-concurrent-resolution",
        "hres": "house-resolution",
        "sres": "senate-resolution",
    }.get(bill_type, "house-bill")
    if congress and number:
        return f"https://www.congress.gov/bill/{ordinal}-congress/{slug}/{number}"
    return "https://www.congress.gov/search"
