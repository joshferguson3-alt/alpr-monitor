"""CourtListener REST API v4.

GET https://www.courtlistener.com/api/rest/v4/search/?q=...&type=o
Authorization: Token <token>. Results are limited to the Southeast
(AL, FL, GA, KY, LA, MS, NC, SC, TN, VA) and the 4th, 5th, 6th, and 11th Circuits.
"""

from __future__ import annotations

import os
from datetime import date
from urllib.parse import quote

from monitor.collectors.base import MissingKeyError, RawDocument
from monitor.http import HttpClient

# CourtListener ids use state prefixes (gamd, flsd, vaed) and ca4/ca5/ca6/ca11.
SOUTHEAST_PREFIXES = (
    "al", "fl", "ga", "ky", "la", "ms", "nc", "sc", "tn", "va",
    "ca4", "ca5", "ca6", "ca11",
)


class CourtListenerCollector:
    def collect(self, source: dict, *, since: date, until: date, http: HttpClient) -> list[RawDocument]:
        token = (os.environ.get("COURTLISTENER_TOKEN") or "").strip()
        if not token:
            # Logged by the runner as a skip. An empty secret must not call the API.
            raise MissingKeyError("COURTLISTENER_TOKEN")
        query = source.get("query") or '"license plate reader" OR ALPR OR "Flock Safety"'
        url = (
            "https://www.courtlistener.com/api/rest/v4/search/?q="
            + quote(query)
            + "&type=o&order_by=dateFiled+desc"
        )
        payload = http.get(url, headers={"Authorization": f"Token {token}", "Accept": "application/json"}).json()
        documents: list[RawDocument] = []
        for item in payload.get("results") or []:
            court = (item.get("court_id") or item.get("court") or "").lower()
            if court == "scotus":
                continue
            if court and not any(court.startswith(prefix) for prefix in SOUTHEAST_PREFIXES):
                continue
            filed = (item.get("dateFiled") or item.get("date_filed") or "")[:10]
            if filed:
                try:
                    stamp = date.fromisoformat(filed)
                except ValueError:
                    stamp = None
                if stamp and stamp < since:
                    continue
            title = item.get("caseName") or item.get("case_name") or "Opinion"
            snippet = item.get("snippet") or item.get("text") or ""
            link = item.get("absolute_url") or ""
            if link.startswith("/"):
                link = "https://www.courtlistener.com" + link
            if not link:
                continue
            documents.append(
                RawDocument(
                    source_id=source["id"],
                    url=link,
                    title=title,
                    published_at=filed or None,
                    doc_type="court",
                    text=f"{title}\n{snippet}",
                )
            )
            if len(documents) >= int(source.get("max_docs") or 15):
                break
        return documents
