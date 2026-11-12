"""RSS and Atom feeds."""

from __future__ import annotations

from datetime import date, datetime
from time import struct_time

import feedparser

from monitor.collectors.base import RawDocument
from monitor.http import HttpClient


def _published(entry) -> date | None:
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if isinstance(parsed, struct_time):
        return date(parsed.tm_year, parsed.tm_mon, parsed.tm_mday)
    return None


class RssCollector:
    def collect(self, source: dict, *, since: date, until: date, http: HttpClient) -> list[RawDocument]:
        response = http.get(source["url"])
        parsed = feedparser.parse(response.content)
        documents: list[RawDocument] = []
        for entry in parsed.entries:
            published = _published(entry)
            if published and (published < since or published > until):
                continue
            url = entry.get("link") or ""
            if not url:
                continue
            title = entry.get("title") or source["id"]
            summary = entry.get("summary") or entry.get("description") or ""
            from bs4 import BeautifulSoup

            text = BeautifulSoup(f"{title}\n{summary}", "html.parser").get_text("\n", strip=True)
            documents.append(
                RawDocument(
                    source_id=source["id"],
                    url=url,
                    title=title,
                    published_at=published.isoformat() if published else None,
                    doc_type=source.get("doc_type") or "news",
                    text=text,
                )
            )
            if len(documents) >= int(source.get("max_docs") or 20):
                break
        return documents
