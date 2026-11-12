"""Cut ~1,500 characters on each side of a keyword hit and merge overlaps."""

from __future__ import annotations

from monitor.prefilter import Hit

WINDOW = 1500


def extract_passages(text: str, hits: list[Hit], window: int = WINDOW) -> list[str]:
    if not text or not hits:
        return []
    ranges: list[tuple[int, int]] = []
    for hit in hits:
        start = max(0, hit.start - window)
        end = min(len(text), hit.end + window)
        ranges.append((start, end))
    ranges.sort()
    merged: list[tuple[int, int]] = [ranges[0]]
    for start, end in ranges[1:]:
        left, right = merged[-1]
        if start <= right:
            merged[-1] = (left, max(right, end))
        else:
            merged.append((start, end))
    return [text[start:end].strip() for start, end in merged if text[start:end].strip()]
