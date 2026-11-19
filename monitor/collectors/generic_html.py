"""Config-driven HTML listing collector."""

from __future__ import annotations

from datetime import date
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from monitor.collectors.base import RawDocument
from monitor.http import HttpClient
from monitor.pdftext import extract_pdf_text


class GenericHtmlCollector:
    def collect(self, source: dict, *, since: date, until: date, http: HttpClient) -> list[RawDocument]:
        response = http.get(source["url"])
        content_type = response.headers.get("content-type", "")
        if "pdf" in content_type:
            text = extract_pdf_text(response.content)
            return [
                RawDocument(
                    source_id=source["id"],
                    url=source["url"],
                    title=source.get("title") or source["id"],
                    published_at=None,
                    doc_type=source.get("doc_type") or "packet",
                    text=text,
                )
            ] if text.strip() else []
        soup = BeautifulSoup(response.text, "html.parser")
        if source.get("page_is_document"):
            text = soup.get_text("\n", strip=True)
            title = source.get("title") or (soup.title.get_text(" ", strip=True) if soup.title else source["id"])
            if not text.strip():
                return []
            return [
                RawDocument(
                    source_id=source["id"],
                    url=source["url"],
                    title=str(title)[:300],
                    published_at=source.get("published_at"),
                    doc_type=source.get("doc_type") or "news",
                    text=text[:200_000],
                )
            ]
        selectors = source.get("selectors") or {}
        item_sel = selectors.get("item") or "article, li, .item"
        title_sel = selectors.get("title") or "a"
        link_sel = selectors.get("link") or "a"
        needle = (source.get("link_contains") or "").lower()
        documents: list[RawDocument] = []
        seen: set[str] = set()
        for node in soup.select(item_sel):
            link = node.select_one(link_sel) if hasattr(node, "select_one") else None
            if link is None and getattr(node, "name", None) == "a":
                link = node
            if link is None or not link.get("href"):
                continue
            href = link["href"].strip()
            if href.startswith("#") or href.lower().startswith("javascript"):
                continue
            if needle and needle not in href.lower():
                continue
            url = urljoin(source["url"], href)
            if url in seen:
                continue
            seen.add(url)
            title_node = node.select_one(title_sel) if node is not link else link
            title = (title_node.get_text(" ", strip=True) if title_node else "") or link.get_text(" ", strip=True)
            if not title:
                continue
            # Listing pages are the document when follow is false.
            if source.get("follow") is False:
                text = node.get_text("\n", strip=True)
            else:
                # Stay on the listing text unless the link looks like a document.
                if any(token in url.lower() for token in (".pdf", "viewfile", "fileopen", "agenda", "viewmeeting")):
                    body = http.get(url)
                    if "pdf" in body.headers.get("content-type", "") or url.lower().endswith(".pdf"):
                        text = extract_pdf_text(body.content)
                    else:
                        text = BeautifulSoup(body.text, "html.parser").get_text("\n", strip=True)
                else:
                    text = f"{title}\n{node.get_text(' ', strip=True)}"
            if not text.strip():
                continue
            documents.append(
                RawDocument(
                    source_id=source["id"],
                    url=url,
                    title=title[:300],
                    published_at=None,
                    doc_type=source.get("doc_type") or "news",
                    text=text[:200_000],
                )
            )
            if len(documents) >= int(source.get("max_docs") or 15):
                break
        return documents
