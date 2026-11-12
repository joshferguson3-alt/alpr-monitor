"""Flock transparency portal snapshots.

A change between the stored snapshot and the page just fetched becomes one
document. The live portal returned HTTP 403 on 2026-11-12, so a configured
URL that forbids access is logged and skipped by the runner.
"""

from __future__ import annotations

import re
from datetime import date

from bs4 import BeautifulSoup

from monitor.collectors.base import RawDocument
from monitor.http import HttpClient

FIELD_PATTERNS = {
    "camera_count": r"(?:cameras|camera count)\s*[:\-]\s*(\d+)",
    "retention_days": r"retention(?:\s+period)?\s*[:\-]\s*(\d+)",
    "searches": r"(?:searches|search volume)\s*[:\-]\s*(\d+)",
    "sharing": r"(?:sharing partners|shared with)\s*[:\-]\s*([^\n]+)",
}


def extract_fields(html_or_text: str) -> dict:
    if "<" in html_or_text and ">" in html_or_text:
        text = BeautifulSoup(html_or_text, "html.parser").get_text("\n", strip=True)
    else:
        text = html_or_text
    fields: dict[str, str] = {}
    for name, pattern in FIELD_PATTERNS.items():
        match = re.search(pattern, text, re.I)
        if match:
            fields[name] = match.group(1).strip()
    return fields


def describe_change(previous: dict | None, current: dict) -> str | None:
    if previous is None:
        return None
    changes = []
    keys = sorted(set(previous) | set(current))
    for key in keys:
        old = previous.get(key)
        new = current.get(key)
        if old != new:
            changes.append(f"{key} changed from {old} to {new}")
    if not changes:
        return None
    return "Flock Safety transparency portal change. " + "; ".join(changes) + "."


class FlockTransparencyCollector:
    """Fetch the portal, store fields on the source, and emit one document when they change."""

    def collect(self, source: dict, *, since: date, until: date, http: HttpClient) -> list[RawDocument]:
        if not source.get("url"):
            return []
        response = http.get(source["url"])
        fields = extract_fields(response.text)
        source["_new_snapshot"] = fields
        change = describe_change(source.get("_previous_snapshot"), fields)
        if not change:
            return []
        stamp = until.isoformat()
        return [
            RawDocument(
                source_id=source["id"],
                url=f"{source['url']}#change-{stamp}",
                title="Flock transparency portal change",
                published_at=stamp,
                doc_type="portal",
                text=change,
            )
        ]
