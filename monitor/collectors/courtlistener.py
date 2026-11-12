"""CourtListener REST API v4.

GET https://www.courtlistener.com/api/rest/v4/search/?q=...&type=o
Authorization: Token <token>. Results are limited to Georgia, Tennessee,
and the 11th and 6th Circuits.
"""

from __future__ import annotations

import os
from datetime import date
from urllib.parse import quote

from monitor.collectors.base import MissingKeyError, RawDocument
from monitor.http import HttpClient

COURTS = {
    "gasd", "gamd", "gand", "ga", "gactapp",
    "tnmd", "tned", "tnwd", "tn", "tnctapp",
    "ca11", "ca6", "scotus",
}


class CourtListenerCollector:
    def collect(self, source: dict, *, since: date, until: date, http: HttpClient) -> list[RawDocument]:
        token = os.environ.get("COURTLISTENER_TOKEN")
        if not token:
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
            if court and court not in COURTS and not any(court.startswith(prefix) for prefix in ("ga", "tn", "ca11", "ca6")):
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
