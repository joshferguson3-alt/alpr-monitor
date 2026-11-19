"""Bill identity and status, shared by LegiScan and Open States."""

from __future__ import annotations

import re

_LEGISCAN = re.compile(
    r"legiscan\.com/([A-Za-z]{2})/(?:bill|text)/([A-Za-z]+\d+)/(\d{4})",
    re.I,
)
_OPENSTATES = re.compile(
    r"openstates\.org/([A-Za-z]{2})/bills/(?:(\d{4})/)?([A-Za-z]+\s*\d+)",
    re.I,
)
_NUMBER = re.compile(r"\b((?:HB|SB|AB|HF|SF|S|H)\s*\d+)\b", re.I)
_SOURCE_STATE = re.compile(r"(?:^|-)([a-z]{2})-bills$", re.I)


def normalize_number(raw: str) -> str:
    compact = re.sub(r"\s+", "", raw).upper()
    match = re.match(r"([A-Z]+?)0*(\d+)$", compact)
    if not match:
        return compact
    return f"{match.group(1)}{int(match.group(2))}"


def bill_key(
    source_id: str | None,
    url: str | None,
    title: str | None,
    explicit: str | None = None,
    session: str | None = None,
) -> str | None:
    """state:NUMBER:year so LegiScan and Open States share one row per bill."""
    for pattern in (_LEGISCAN, _OPENSTATES):
        match = pattern.search(url or "")
        if not match:
            continue
        state = match.group(1).upper()
        if pattern is _LEGISCAN:
            number, year = match.group(2), match.group(3)
        else:
            year = match.group(2) or (session or "")[:4]
            number = match.group(3)
        if not year:
            return None
        return f"{state}:{normalize_number(number)}:{year}"
    state_match = _SOURCE_STATE.search(source_id or "")
    number_match = _NUMBER.search(f"{explicit or ''} {title or ''}")
    year = (session or "")[:4]
    if state_match and number_match and year.isdigit():
        return f"{state_match.group(1).upper()}:{normalize_number(number_match.group(1))}:{year}"
    return None


def status_text(blob: str) -> str:
    return (blob or "").lower()


def is_local_appropriation(blob: str) -> bool:
    text = status_text(blob)
    if "court technology" in text:
        return True
    if "local bill" in text:
        return True
    if "city of " in text or "county of " in text:
        return "appropriat" in text or "license plate" in text or "lpr" in text or "alpr" in text
    return False


def status_kind(blob: str) -> str:
    text = status_text(blob)
    if any(phrase in text for phrase in ("died", "failed", "withdrawn", "indefinitely postponed", "vetoed")):
        return "dead"
    if any(
        phrase in text
        for phrase in (
            "signed by the governor",
            "signed into law",
            "governor signed",
            "bill was signed",
            "chaptered",
            "session law",
            "s.l.",
            "became law",
            "ratified",
        )
    ):
        return "enacted"
    if any(
        phrase in text
        for phrase in (
            "passed the house",
            "passed the senate",
            "passed a chamber",
            "passed the chamber",
            "passed 2nd",
            "passed 3rd",
            "passed second",
            "passed third",
            "engrossed",
        )
    ):
        return "passed"
    return "pending"


def is_program_or_ban(blob: str) -> bool:
    text = status_text(blob)
    return any(phrase in text for phrase in ("retention", "ban", "prohibit", "program", "session law", "s.l."))
