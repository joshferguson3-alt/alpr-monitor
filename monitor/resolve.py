"""Match extracted names to registry ids. Unmatched names stay as text."""

from __future__ import annotations

import re

from monitor.registry import Registry


def _norm(value: str | None) -> str:
    if not value:
        return ""
    text = value.lower()
    text = text.replace("&", " and ")
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    for token in ("police department", "police", "pd", "city of", "county", "the"):
        text = re.sub(rf"\b{token}\b", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def resolve_agency(name: str | None, registry: Registry) -> tuple[str | None, bool]:
    """Return (agency_id, needs_review). A raw name that matches nothing is flagged."""
    if not name:
        return None, False
    target = _norm(name)
    for agency in registry.agencies:
        names = [agency["name"], agency["id"], *list(agency.get("aliases") or [])]
        if any(_norm(candidate) == target or (_norm(candidate) and _norm(candidate) in target) for candidate in names):
            return agency["id"], False
    return None, True


def resolve_person(name: str | None, registry: Registry) -> tuple[str | None, bool]:
    if not name:
        return None, False
    target = _norm(name)
    for person in registry.people:
        if _norm(person["name"]) == target:
            return person["id"], False
    return None, True


def resolve_organization(name: str | None, registry: Registry) -> tuple[str | None, bool]:
    if not name:
        return None, False
    target = _norm(name)
    for org in registry.organizations:
        if _norm(org["name"]) == target or target and target in _norm(org["name"]):
            return org["id"], False
    return None, True
