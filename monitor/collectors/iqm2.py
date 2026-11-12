"""IQM2 citizens portal (Roswell). robots.txt sets Crawl-delay: 60 and allows the portal.

Agenda files are FileOpen.aspx?Type=14. Packets (Type=1) are skipped so a run
stays inside the crawl delay. Verified 2026-11-12 at roswellcityga.iqm2.com.
"""

from __future__ import annotations

import re
from datetime import date
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from monitor.collectors.base import RawDocument
from monitor.http import HttpClient
from monitor.pdftext import extract_pdf_text

_DATE = re.compile(
    r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2}),\s+(\d{4})",
    re.I,
)
_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
}


class Iqm2Collector:
    def collect(self, source: dict, *, since: date, until: date, http: HttpClient) -> list[RawDocument]:
        timeout = source.get("timeout")
        retries = source.get("retries")
        response = http.get(source["url"], timeout=timeout, retries=retries)
        soup = BeautifulSoup(response.text, "html.parser")
        bodies = {name.lower() for name in source.get("bodies") or []}
        documents: list[RawDocument] = []
        for row in soup.select("div.RowTop"):
            link = row.select_one("div.RowLink a")
            if link is None:
                continue
            title_attr = link.get("title") or ""
            board_match = re.search(r"Board:\s*([^\r\n]+)", title_attr)
            board = board_match.group(1).strip() if board_match else ""
            if bodies and board.lower() not in bodies:
                continue
            date_match = _DATE.search(title_attr) or _DATE.search(link.get_text(" ", strip=True))
            meeting_date = None
            if date_match:
                meeting_date = date(
                    int(date_match.group(3)),
                    _MONTHS[date_match.group(1).lower()],
                    int(date_match.group(2)),
                )
            if meeting_date and (meeting_date < since or meeting_date > until):
                continue
            agenda = None
            for anchor in row.select("a[href]"):
                if "hidden" in " ".join(anchor.get("class") or []).lower():
                    continue
                if anchor.get_text(strip=True).lower() == "agenda" and "FileOpen.aspx" in anchor["href"]:
                    agenda = anchor
                    break
            if agenda is None:
                continue
            url = urljoin(source["url"], agenda["href"])
            body = http.get(url, timeout=timeout, retries=retries)
            content_type = body.headers.get("content-type", "")
            if "pdf" in content_type or body.content[:4] == b"%PDF":
                text = extract_pdf_text(body.content)
            else:
                text = BeautifulSoup(body.text, "html.parser").get_text("\n", strip=True)
            if not text.strip():
                continue
            label = f"{board} {meeting_date.isoformat()}" if meeting_date else board or "Meeting"
            documents.append(
                RawDocument(
                    source_id=source["id"],
                    url=url,
                    title=label.strip(),
                    published_at=meeting_date.isoformat() if meeting_date else None,
                    doc_type=source.get("doc_type") or "agenda",
                    text=text[:200_000],
                )
            )
            if len(documents) >= int(source.get("max_docs") or 3):
                break
        return documents
