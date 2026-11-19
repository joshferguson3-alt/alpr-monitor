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
        if (source.get("mode") or "").lower() == "onbase":
            return _onbase(source, since=since, until=until, http=http)
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
                if any(token in url.lower() for token in (".pdf", "viewfile", "fileopen", "agenda", "viewmeeting", "downloadfile")):
                    body = http.get(url)
                    if "pdf" in body.headers.get("content-type", "") or url.lower().endswith(".pdf") or body.content[:4] == b"%PDF":
                        text = extract_pdf_text(body.content)
                    else:
                        text = BeautifulSoup(body.text, "html.parser").get_text("\n", strip=True)
                else:
                    text = f"{title}\n{node.get_text(' ', strip=True)}"
            if len(text) < 300 and source.get("doc_type") in {"agenda", "minutes", "packet"}:
                text = _follow_thin(http, node, url, text)
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


def _follow_thin(http: HttpClient, node, url: str, text: str) -> str:
    """A short agenda title is not the packet. Follow attachment links."""
    candidates = [url]
    for anchor in node.select("a[href]"):
        href = anchor.get("href") or ""
        blob = f"{anchor.get_text(' ', strip=True)} {href}".lower()
        if any(token in blob for token in ("packet", ".pdf", "downloadfile", "viewfile", "attachment")):
            candidates.append(urljoin(url, href))
    best = text
    for candidate in candidates:
        try:
            body = http.get(candidate)
        except Exception:
            continue
        if "pdf" in body.headers.get("content-type", "") or candidate.lower().endswith(".pdf") or body.content[:4] == b"%PDF":
            extracted = extract_pdf_text(body.content)
        else:
            extracted = BeautifulSoup(body.text, "html.parser").get_text("\n", strip=True)
        if len(extracted) > len(best):
            best = extracted
        if len(best) >= 300:
            break
    return best


def packet_links(html: str, base: str) -> list[tuple[str, str]]:
    """Agenda-packet URLs from an OnBase search result, newest meeting id first."""
    soup = BeautifulSoup(html, "html.parser")
    found: dict[str, str] = {}
    for anchor in soup.select("a[href]"):
        href = anchor.get("href") or ""
        blob = f"{anchor.get_text(' ', strip=True)} {href}"
        if "documentType=5" not in href and "Agenda_Packet" not in href and "Agenda Packet" not in blob:
            continue
        meeting = ""
        for token in href.split("&"):
            if "meetingId=" in token:
                meeting = token.split("meetingId=")[-1]
        if not meeting:
            continue
        found[meeting] = urljoin(base, href)
    return [(meeting, found[meeting]) for meeting in sorted(found, key=lambda item: int(item) if item.isdigit() else 0, reverse=True)]


def _onbase(source: dict, *, since: date, until: date, http: HttpClient) -> list[RawDocument]:
    page = http.get(source["url"])
    soup = BeautifulSoup(page.text, "html.parser")
    token_node = soup.find("input", {"name": "__RequestVerificationToken"})
    token = token_node.get("value") if token_node else ""
    types = source.get("meeting_types") or ["106", "107"]
    # 7 = this year, 1 = last year. Together they cover a 365-day backfill.
    html_pages = []
    for option in ("7", "1"):
        for meeting_type in types:
            data = {
                "__RequestVerificationToken": token,
                "Keywords": "",
                "MeetingTypeIDs": str(meeting_type),
                "DateRangeOptionID": option,
                "DateRangeCustomStartDate": f"{since.month}/{since.day}/{since.year}",
                "DateRangeCustomEndDate": f"{until.month}/{until.day}/{until.year}",
            }
            result = http.post(source["url"], data=data)
            html_pages.append(result.text)
    chosen: dict[str, str] = {}
    for html in html_pages:
        for meeting, url in packet_links(html, source["url"]):
            chosen.setdefault(meeting, url)
    documents: list[RawDocument] = []
    ordered = sorted(chosen, key=lambda item: int(item) if item.isdigit() else 0, reverse=True)
    for meeting in ordered:
        url = chosen[meeting]
        body = http.get(url)
        if body.content[:4] == b"%PDF" or "pdf" in body.headers.get("content-type", "") or url.lower().endswith(".pdf"):
            text = extract_pdf_text(body.content)
        else:
            text = BeautifulSoup(body.text, "html.parser").get_text("\n", strip=True)
        if not text.strip():
            continue
        documents.append(
            RawDocument(
                source_id=source["id"],
                url=url,
                title=f"Meeting {meeting}",
                published_at=None,
                doc_type=source.get("doc_type") or "agenda",
                text=text[:200_000],
            )
        )
        if len(documents) >= int(source.get("max_docs") or 6):
            break
    return documents
