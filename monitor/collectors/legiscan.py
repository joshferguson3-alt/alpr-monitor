"""LegiScan public pull API.

GET https://api.legiscan.com/?key=KEY&op=getSearch&state=GA&query=QUERY&year=2
then op=getBill&id=BILL_ID and op=getBillText&id=DOC_ID. Public tier is
10,000 queries per month. https://legiscan.com/legiscan, checked 2026-11-12.
"""

from __future__ import annotations

import base64
import os
from datetime import date
from urllib.parse import quote

from bs4 import BeautifulSoup

from monitor.collectors.base import MissingKeyError, RawDocument
from monitor.constants import BILL_QUERY
from monitor.http import HttpClient
from monitor.pdftext import extract_pdf_text

QUERIES = (BILL_QUERY,)


class LegiScanCollector:
    def collect(self, source: dict, *, since: date, until: date, http: HttpClient) -> list[RawDocument]:
        key = os.environ.get("LEGISCAN_API_KEY")
        if not key:
            raise MissingKeyError("LEGISCAN_API_KEY")
        state = source["state"]
        seen: set[int] = set()
        documents: list[RawDocument] = []
        queries = (source["query"],) if source.get("query") else QUERIES
        limit = int(source.get("max_docs") or 30)
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
                body, _have_text = _bill_body(http, key, bill)
                text = _metadata(bill, item)
                if body:
                    text = text + "\n" + body
                last_action = item.get("last_action")
                last_action_date = last_action.get("date") if isinstance(last_action, dict) else None
                change = item.get("last_action_date") or item.get("status_date") or last_action_date or ""
                published = str(change)[:10] if change else None
                # year=2 is already the current session. Do not drop a match
                # because its last action is older than this run's window.
                url_out = item.get("url") or bill.get("url") or f"https://legiscan.com/{state}/bill/{item.get('bill_number')}"
                # The search already matched. A short title or a text file that did
                # not decode must not be dropped before classification.
                documents.append(
                    RawDocument(
                        source_id=source["id"],
                        url=url_out,
                        title=f"{item.get('bill_number') or ''} {item.get('title') or bill.get('title') or ''}".strip(),
                        published_at=published,
                        doc_type="bill",
                        text=text,
                        force_prefilter=True,
                    )
                )
                if len(documents) >= limit:
                    return documents
        return documents


def _metadata(bill: dict, item: dict) -> str:
    sponsors = []
    for sponsor in bill.get("sponsors") or []:
        name = sponsor.get("name") or ""
        if name:
            sponsors.append(name)
    history = []
    for action in (bill.get("history") or [])[-8:]:
        history.append(f"{action.get('date') or ''} {action.get('action') or ''}".strip())
    last_action = history[-1] if history else ""
    parts = [
        item.get("bill_number") or bill.get("bill_number") or "",
        item.get("title") or bill.get("title") or "",
        item.get("description") or bill.get("description") or "",
        f"Bill status: {last_action}" if last_action else "",
        "Sponsors: " + ", ".join(sponsors) if sponsors else "",
        "\n".join(history),
    ]
    return "\n".join(part for part in parts if part)


def _bill_body(http: HttpClient, key: str, bill: dict) -> tuple[str, bool]:
    """Return (text, True) when the latest bill text was decoded."""
    texts = []
    for item in bill.get("texts") or []:
        if not isinstance(item, dict):
            continue
        doc_id = item.get("doc_id") or item.get("text_id")
        if doc_id:
            texts.append((item.get("date") or "", doc_id))
    if not texts:
        return "", False
    best = ""
    for _date, doc_id in sorted(texts, reverse=True):
        decoded = _decode_bill_text(http, key, doc_id)
        if len(decoded) > len(best):
            best = decoded
        if len(best) >= 800:
            break
    return best, bool(best)


def _decode_bill_text(http: HttpClient, key: str, doc_id) -> str:
    try:
        payload = http.get(
            f"https://api.legiscan.com/?key={quote(key)}&op=getBillText&id={doc_id}"
        ).json()
    except Exception:
        return ""
    if payload.get("status") != "OK":
        return ""
    record = payload.get("text") or {}
    encoded = record.get("doc") or record.get("text") or ""
    if not encoded:
        return ""
    try:
        raw = base64.b64decode(encoded)
    except Exception:
        return ""
    mime = (record.get("mime") or "").lower()
    if "pdf" in mime or raw[:4] == b"%PDF":
        return extract_pdf_text(raw).strip()
    return BeautifulSoup(raw, "html.parser").get_text("\n", strip=True).strip()
