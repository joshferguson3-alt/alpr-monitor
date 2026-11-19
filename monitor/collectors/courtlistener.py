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
from monitor.constants import BILL_QUERY
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
        query = source.get("query") or BILL_QUERY
        documents: list[RawDocument] = []
        seen: set[str] = set()
        per_type = int(source.get("max_docs") or 20)
        # Opinions (type=o) and RECAP dockets (type=r). Each type gets its own cap
        # so a long opinion list cannot hide docket hits.
        for result_type in ("o", "r"):
            url = (
                "https://www.courtlistener.com/api/rest/v4/search/?q="
                + quote(query)
                + f"&type={result_type}&order_by=dateFiled+desc"
            )
            pages = 0
            while url and pages < 2:
                payload = http.get(url, headers={"Authorization": f"Token {token}", "Accept": "application/json"}).json()
                pages += 1
                before = len([doc for doc in documents if doc.title.startswith("Docket" if result_type == "r" else "Opinion")])
                _extend_court_results(
                    documents,
                    seen,
                    payload.get("results") or [],
                    source_id=source["id"],
                    result_type=result_type,
                    since=since,
                    limit=len(documents) + (per_type - before),
                )
                taken = len([doc for doc in documents if doc.title.startswith("Docket" if result_type == "r" else "Opinion")])
                url = payload.get("next") if taken < per_type else None
        return documents


def _extend_court_results(
    documents: list[RawDocument],
    seen: set[str],
    results: list,
    *,
    source_id: str,
    result_type: str,
    since: date,
    limit: int,
) -> None:
    for item in results:
        if len(documents) >= limit:
            return
        court = (item.get("court_id") or item.get("court") or "").lower()
        if isinstance(court, dict):
            court = (court.get("id") or "").lower()
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
        title = item.get("caseName") or item.get("case_name") or item.get("caseNameFull") or "Matter"
        snippet = item.get("snippet") or item.get("text") or ""
        link = item.get("absolute_url") or ""
        if link.startswith("/"):
            link = "https://www.courtlistener.com" + link
        if not link or link in seen:
            continue
        seen.add(link)
        label = "Docket" if result_type == "r" else "Opinion"
        documents.append(
            RawDocument(
                source_id=source_id,
                url=link,
                title=f"{label}: {title}",
                published_at=filed or None,
                doc_type="court",
                text=f"{label}: {title}\n{snippet}",
            )
        )
