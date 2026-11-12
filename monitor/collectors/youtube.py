"""YouTube channel feeds and captions.

Channel videos come from the public Atom feed
https://www.youtube.com/feeds/videos.xml?channel_id=UC...
Captions use youtube-transcript-api. The pipeline fetches captions only for
videos whose matching agenda passed the keyword prefilter.
"""

from __future__ import annotations

import re
from datetime import date

import feedparser

from monitor.collectors.base import RawDocument
from monitor.http import HttpClient


def video_id_from_url(url: str) -> str | None:
    match = re.search(r"(?:v=|youtu\.be/|/shorts/)([A-Za-z0-9_-]{6,})", url)
    return match.group(1) if match else None


def fetch_caption_text(video_id: str) -> str:
    from youtube_transcript_api import YouTubeTranscriptApi

    fetched = YouTubeTranscriptApi().fetch(video_id, languages=("en",))
    parts = []
    for snippet in fetched:
        text = getattr(snippet, "text", None)
        if text is None and isinstance(snippet, dict):
            text = snippet.get("text")
        if text:
            parts.append(text)
    return " ".join(parts)


class YouTubeCollector:
    def __init__(self, caption_fetcher=fetch_caption_text):
        self.caption_fetcher = caption_fetcher

    def list_videos(self, source: dict, *, since: date, until: date, http: HttpClient) -> list[RawDocument]:
        channel_id = source.get("channel_id") or _channel_id(source.get("url") or "", http)
        if not channel_id:
            raise RuntimeError("youtube source needs a channel id or a page that publishes one")
        feed_url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
        parsed = feedparser.parse(http.get(feed_url).content)
        documents: list[RawDocument] = []
        for entry in parsed.entries:
            published = None
            stamp = entry.get("published_parsed") or entry.get("updated_parsed")
            if stamp:
                published = date(stamp.tm_year, stamp.tm_mon, stamp.tm_mday)
            if published and (published < since or published > until):
                continue
            url = entry.get("link") or ""
            if not url:
                continue
            documents.append(
                RawDocument(
                    source_id=source["id"],
                    url=url,
                    title=entry.get("title") or "Video",
                    published_at=published.isoformat() if published else None,
                    doc_type="transcript",
                    text=entry.get("title") or "",
                )
            )
        return documents

    def collect(self, source: dict, *, since: date, until: date, http: HttpClient) -> list[RawDocument]:
        # Captions are attached later, only when an agenda matched.
        return self.list_videos(source, since=since, until=until, http=http)

    def with_captions(self, document: RawDocument) -> RawDocument:
        video_id = video_id_from_url(document.url)
        if not video_id:
            return document
        caption = self.caption_fetcher(video_id)
        document.text = f"{document.title}\n{caption}".strip()
        return document


# Channel ids are UC plus 22 characters. A longer match is not an id.
CHANNEL_ID_RE = re.compile(r"(?<![A-Za-z0-9_-])(UC[A-Za-z0-9_-]{22})(?![A-Za-z0-9_-])")


def _channel_id(url: str, http: HttpClient) -> str | None:
    match = CHANNEL_ID_RE.search(url)
    if match:
        return match.group(1)
    if not url:
        return None
    page = http.get(url).text
    found = CHANNEL_ID_RE.search(page)
    return found.group(1) if found else None
