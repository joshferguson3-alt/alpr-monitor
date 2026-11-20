"""Whether a signal helps or hurts license plate readers, in plain language."""

from __future__ import annotations

POSITIVE = "Positive for ALPR"
NEGATIVE = "Negative for ALPR"
MIXED = "Mixed for ALPR"

_RESTRICTIONS = (
    "ban ",
    "bans ",
    "banned",
    "prohibit",
    "cancel",
    "cancellation",
    "not renew",
    "non-renew",
    "nonrenew",
    "terminat",
    "retention",
    "warrant",
    "ballot",
    "reject",
    "lawsuit",
    "civil action",
    "challenge",
    "restrict",
    "limits data",
    "limit data",
    "limitations on",
    "capped",
    "cap ",
)
_EXPANSIONS = (
    "authoriz",
    "expand",
    "expansion",
    "new contract",
    "appropriat",
    "funding",
    "permanent",
    "program",
    "renew",
    "purchase",
    "install",
    "deploy",
    "grant ",
)


def derive_alpr_stance(*parts: str | None) -> str:
    """Prefer a clear positive or negative. Renewal plus new limits is mixed."""
    text = " ".join(part or "" for part in parts).lower()
    restricted = any(phrase in text for phrase in _RESTRICTIONS)
    expanded = any(phrase in text for phrase in _EXPANSIONS)
    if restricted and expanded:
        return MIXED
    if restricted:
        return NEGATIVE
    if expanded:
        return POSITIVE
    return MIXED


def badge(stance: str | None) -> str:
    label = stance or MIXED
    if label in {POSITIVE, "Neutral / Mixed for ALPR"} and label != POSITIVE:
        return MIXED
    if label == POSITIVE:
        return POSITIVE
    if label == NEGATIVE:
        return NEGATIVE
    if "negative" in label.lower():
        return NEGATIVE
    if "positive" in label.lower():
        return POSITIVE
    return MIXED
