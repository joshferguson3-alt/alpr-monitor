"""Legistar web API. Public read, no key.

GET https://webapi.legistar.com/v1/{client}/events
OData $filter on EventDate. Verified against the help index at
https://webapi.legistar.com/Help on 2026-11-12. The Roswell client on that
API still serves 2014 events, so Roswell's current meetings use IQM2 instead.
"""

from __future__ import annotations

from datetime import date
from urllib.parse import quote

from monitor.collectors.base import RawDocument
from monitor.http import HttpClient
from monitor.pdftext import extract_pdf_text


class LegistarCollector:
    def collect(self, source: dict, *, since: date, until: date, http: HttpClient) -> list[RawDocument]:
        client = source.get("client") or source.get("legistar_client")
        if not client:
            raise RuntimeError("legistar source needs a client slug")
        filt = quote(
            f"EventDate ge datetime'{since.isoformat()}' and EventDate le datetime'{until.isoformat()}'",
            safe="'$:=",
        )
        url = (
            f"https://webapi.legistar.com/v1/{client}/events"
            f"?$filter={filt}&$orderby=EventDate%20desc&$top=40"
        )
        events = http.get(url).json()
        if isinstance(events, dict):
            events = events.get("value") or []
        documents: list[RawDocument] = []
        for event in events:
            file_url = event.get("EventAgendaFile") or ""
            event_id = event.get("EventId")
            public = file_url or f"https://{client}.legistar.com/MeetingDetail.aspx?ID={event_id}&GUID={event.get('EventGuid')}"
            if not file_url:
                continue
            body = http.get(file_url)
            if body.content[:4] == b"%PDF" or "pdf" in body.headers.get("content-type", ""):
                text = extract_pdf_text(body.content)
            else:
                text = body.text
            if not text.strip():
                continue
            published = (event.get("EventDate") or "")[:10] or None
            title = f"{event.get('EventBodyName') or 'Meeting'} {published or ''}".strip()
            documents.append(
                RawDocument(
                    source_id=source["id"],
                    url=public,
                    title=title,
                    published_at=published,
                    doc_type=source.get("doc_type") or "agenda",
                    text=text[:200_000],
                )
            )
            if len(documents) >= int(source.get("max_docs") or 8):
                break
        return documents
