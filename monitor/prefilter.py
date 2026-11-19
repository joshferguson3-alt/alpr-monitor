"""Keyword prefilter. No model calls."""

from __future__ import annotations

import re
from dataclasses import dataclass

FLOCK_NEIGHBORS = ("camera", "safety", "contract", "license")


@dataclass(frozen=True)
class Hit:
    start: int
    end: int
    term: str
    strong: bool
    context: bool = False


def _spans(pattern: str, text: str, flags: int = re.IGNORECASE) -> list[tuple[int, int]]:
    return [(m.start(), m.end()) for m in re.finditer(pattern, text, flags)]


def flock_weak_spans(text: str) -> list[tuple[int, int]]:
    """Match Flock only when capitalized mid-sentence or beside camera/safety/contract/license."""
    spans: list[tuple[int, int]] = []
    for match in re.finditer(r"\bFlock\b", text):
        before = text[: match.start()]
        mid_sentence = bool(re.search(r"[a-z0-9,;:]\s*$", before))
        window = text[max(0, match.start() - 40) : match.end() + 40].lower()
        neighbor = any(word in window for word in FLOCK_NEIGHBORS)
        if mid_sentence or neighbor:
            spans.append((match.start(), match.end()))
    for match in re.finditer(r"\bflock\b", text):
        window = text[max(0, match.start() - 40) : match.end() + 40].lower()
        if any(word in window for word in FLOCK_NEIGHBORS):
            spans.append((match.start(), match.end()))
    return _merge(spans)


def find_hits(text: str, keywords: dict) -> list[Hit]:
    if not text:
        return []
    hits: list[Hit] = []
    occupied: list[tuple[int, int]] = []

    for term in keywords.get("strong") or []:
        for start, end in _spans(r"\b" + re.escape(term) + r"\b", text):
            hits.append(Hit(start, end, term, True))
            occupied.append((start, end))

    for term in keywords.get("weak") or []:
        if term.lower() == "flock":
            spans = flock_weak_spans(text)
        elif term == "LPR":
            spans = _spans(r"\bLPR\b", text, flags=0)
        else:
            spans = _spans(r"\b" + re.escape(term) + r"\b", text)
        for start, end in spans:
            if any(start < right and end > left for left, right in occupied):
                continue
            hits.append(Hit(start, end, term, False))
    return sorted(hits, key=lambda hit: hit.start)


def _within(a: Hit, b: Hit, limit: int = 300) -> bool:
    gap = max(a.start, b.start) - min(a.end, b.end)
    if gap < 0:
        return True
    return gap <= limit


def qualifying_hits(hits: list[Hit]) -> list[Hit]:
    """Strong hits always qualify. A weak hit qualifies when another hit is within 300 characters."""
    if any(hit.strong for hit in hits):
        strong = [hit for hit in hits if hit.strong]
        weak = [hit for hit in hits if not hit.strong and any(_within(hit, other) for other in hits if other is not hit)]
        # A lone strong term is enough even if weak hits are far away.
        return sorted(strong + weak, key=lambda hit: hit.start)
    qualified = []
    for hit in hits:
        if any(_within(hit, other) for other in hits if other is not hit):
            qualified.append(hit)
    return qualified


def context_hits(text: str, keywords: dict, taken: list[Hit]) -> list[Hit]:
    """Word-boundary matches of context terms that do not overlap an accepted hit."""
    if not text:
        return []
    occupied = [(hit.start, hit.end) for hit in taken]
    hits: list[Hit] = []
    for term in keywords.get("context") or []:
        for start, end in _spans(r"\b" + re.escape(term) + r"\b", text):
            if any(start < right and end > left for left, right in occupied):
                continue
            hits.append(Hit(start, end, term, False, True))
            occupied.append((start, end))
    return hits


def passes(text: str, keywords: dict, include_context: bool = False) -> list[Hit]:
    qualified = qualifying_hits(find_hits(text, keywords))
    if not include_context:
        return qualified
    extra = context_hits(text, keywords, qualified)
    return sorted(qualified + extra, key=lambda hit: hit.start)


def _merge(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    if not spans:
        return []
    spans = sorted(spans)
    merged = [spans[0]]
    for start, end in spans[1:]:
        left, right = merged[-1]
        if start <= right:
            merged[-1] = (left, max(right, end))
        else:
            merged.append((start, end))
    return merged
