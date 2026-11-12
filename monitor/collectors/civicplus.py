"""CivicPlus Agenda Center HTML and CivicClerk public OData API.

Agenda Center listing pages render meeting rows in HTML. CivicClerk portals
(*.portal.civicclerk.com) expose an unauthenticated OData API at
https://{tenant}.api.civicclerk.com/v1/Events. Plain text of a published file
is https://{tenant}.api.civicclerk.com/v1/Meetings/GetMeetingFileStream(fileId=N,plainText=true).
No API key. Verified against sandyspringsga on 2026-11-12.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from urllib.parse import quote, urljoin, urlparse

from bs4 import BeautifulSoup

from monitor.collectors.base import RawDocument
from monitor.http import HttpClient
from monitor.pdftext import extract_pdf_text

MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8, "sep": 9,
    "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _parse_long_date(text: str) -> date | None:
    match = re.search(
        r"(January|February|March|April|May|June|July|August|September|October|November|December|"
        r"Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?\s+(\d{1,2}),?\s+(\d{4})",
        text,
        re.I,
    )
    if not match:
        return None
    month = MONTHS[match.group(1).lower()[:3] if match.group(1).lower()[:3] != "sep" else "sep"]
    # map via the dict using a normalized key
    key = match.group(1).lower().rstrip(".")
    month = MONTHS.get(key) or MONTHS.get(key[:3])
    if not month:
        return None
    return date(int(match.group(3)), month, int(match.group(2)))


class CivicPlusCollector:
    def collect(self, source: dict, *, since: date, until: date, http: HttpClient) -> list[RawDocument]:
        mode = (source.get("mode") or "agenda_center").lower()
        if mode == "civicclerk":
            return self._civicclerk(source, since=since, until=until, http=http)
        return self._agenda_center(source, since=since, until=until, http=http)

    def _agenda_center(self, source: dict, *, since: date, until: date, http: HttpClient) -> list[RawDocument]:
        response = http.get(source["url"])
        soup = BeautifulSoup(response.text, "html.parser")
        documents: list[RawDocument] = []
        for row in soup.select("tr.catAgendaRow"):
            label = ""
            strong = row.select_one("strong")
            if strong and strong.get("aria-label"):
                label = strong["aria-label"]
            meeting_date = _parse_long_date(label) or _parse_long_date(row.get_text(" ", strip=True))
            posted = None
            text = row.get_text(" ", strip=True)
            posted_match = re.search(r"Posted\s+(.+?\d{4})", text)
            if posted_match:
                posted = _parse_long_date(posted_match.group(1))
            stamp = posted or meeting_date
            if stamp and (stamp < since or stamp > until):
                continue
            link = row.select_one('a[href*="ViewFile/Agenda"][href*="html=true"]')
            if link is None:
                link = row.select_one('a[href*="html=true"]')
            if link is None:
                link = row.select_one('a[href*="ViewFile/Agenda"]')
            if link is None:
                link = row.select_one('a[href*="ViewFile/Minutes"]')
            if link is None or not link.get("href"):
                continue
            url = urljoin(source["url"], link["href"])
            title_node = row.select_one("a[href*='html=true'], a[href*='ViewFile/Agenda']")
            title = (title_node.get_text(" ", strip=True) if title_node else "") or (link.get_text(" ", strip=True))
            if meeting_date:
                title = f"{title} ({meeting_date.isoformat()})" if title else meeting_date.isoformat()
            body = http.get(url)
            content_type = body.headers.get("content-type", "")
            if "pdf" in content_type or url.lower().endswith(".pdf"):
                text_body = extract_pdf_text(body.content)
                doc_type = "packet"
            else:
                page = BeautifulSoup(body.text, "html.parser")
                text_body = page.get_text("\n", strip=True)
                doc_type = "agenda"
            if not text_body.strip():
                continue
            documents.append(
                RawDocument(
                    source_id=source["id"],
                    url=url.split("?")[0] if "html=true" in url else url,
                    title=title or source["id"],
                    published_at=(stamp or meeting_date).isoformat() if (stamp or meeting_date) else None,
                    doc_type=source.get("doc_type") or doc_type,
                    text=text_body,
                )
            )
            if len(documents) >= int(source.get("max_docs") or 8):
                break
        return documents

    def _civicclerk(self, source: dict, *, since: date, until: date, http: HttpClient) -> list[RawDocument]:
        tenant = source.get("tenant") or _tenant_from_url(source["url"])
        if not tenant:
            raise RuntimeError("civicclerk source needs a tenant")
        base = f"https://{tenant}.api.civicclerk.com/v1"
        filt = (
            f"startDateTime ge {since.isoformat()}T00:00:00Z "
            f"and startDateTime le {until.isoformat()}T23:59:59Z"
        )
        url = f"{base}/Events?$filter={quote(filt)}&$orderby=startDateTime%20desc&$top=20"
        wanted = {name.lower() for name in source.get("categories") or []}
        documents: list[RawDocument] = []
        pages = 0
        while url and pages < 4:
            payload = http.get(url).json()
            pages += 1
            for event in payload.get("value") or []:
                category = (event.get("categoryName") or event.get("eventCategoryName") or "")
                if wanted and category.lower() not in wanted and not any(item in category.lower() for item in wanted):
                    continue
                files = event.get("publishedFiles") or []
                chosen = _pick_file(files)
                if not chosen:
                    continue
                file_id = chosen.get("fileId")
                file_url = (
                    f"{base}/Meetings/GetMeetingFileStream(fileId={file_id},plainText=true)"
                )
                text = http.get(file_url).text
                if not text.strip():
                    continue
                event_id = event.get("id")
                public = f"https://{tenant}.portal.civicclerk.com/event/{event_id}"
                published = (event.get("startDateTime") or "")[:10] or None
                documents.append(
                    RawDocument(
                        source_id=source["id"],
                        url=public,
                        title=event.get("eventName") or category or "Meeting",
                        published_at=published,
                        doc_type=source.get("doc_type") or "agenda",
                        text=text,
                    )
                )
                if len(documents) >= int(source.get("max_docs") or 8):
                    return documents
            url = payload.get("@odata.nextLink")
        return documents


def _tenant_from_url(url: str) -> str:
    host = urlparse(url).netloc.lower()
    return host.split(".")[0]


def _pick_file(files: list[dict]) -> dict | None:
    for preferred in ("Agenda", "Minutes", "Agenda Packet"):
        for item in files:
            if item.get("type") == preferred and item.get("fileId"):
                return item
    for item in files:
        if item.get("fileId"):
            return item
    return None
