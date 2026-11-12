"""PrimeGov public portal.

Brookhaven's portal (brookhavenga.primegov.com) publishes robots.txt as
``User-agent: *`` / ``Disallow: /``. The HTTP client raises RobotsDenied
before any page fetch. This collector does not try to bypass that.
"""

from __future__ import annotations

from datetime import date
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from monitor.collectors.base import RawDocument
from monitor.http import HttpClient


class PrimeGovCollector:
    def collect(self, source: dict, *, since: date, until: date, http: HttpClient) -> list[RawDocument]:
        response = http.get(source["url"])
        soup = BeautifulSoup(response.text, "html.parser")
        documents: list[RawDocument] = []
        for link in soup.select("a[href]"):
            href = link["href"]
            if not any(token in href.lower() for token in ("meeting", "agenda", "document")):
                continue
            url = urljoin(source["url"], href)
            title = link.get_text(" ", strip=True) or "Meeting"
            body = http.get(url)
            text = BeautifulSoup(body.text, "html.parser").get_text("\n", strip=True)
            if not text.strip():
                continue
            documents.append(
                RawDocument(
                    source_id=source["id"],
                    url=url,
                    title=title[:300],
                    published_at=None,
                    doc_type=source.get("doc_type") or "agenda",
                    text=text[:200_000],
                )
            )
            if len(documents) >= int(source.get("max_docs") or 6):
                break
        return documents
