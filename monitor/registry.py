"""Load and validate the YAML registry."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

COLLECTORS = {
    "legistar",
    "civicplus",
    "generic_html",
    "rss",
    "legiscan",
    "openstates",
    "youtube",
    "flock_transparency",
    "congress",
    "courtlistener",
    "primegov",
    "iqm2",
}

AGENCY_TYPES = {"municipal_police", "county_police", "sheriff"}
CADENCES = {"daily", "weekly", "monthly", "quarterly"}


def segment_for_count(sworn: int | None) -> str | None:
    if sworn is None:
        return None
    if sworn <= 40:
        return "inside"
    if sworn <= 100:
        return "mid_city"
    if sworn <= 550:
        return "t1200"
    return "major"


@dataclass
class Registry:
    agencies: list[dict[str, Any]]
    people: list[dict[str, Any]]
    organizations: list[dict[str, Any]]
    sources: list[dict[str, Any]]
    keywords: dict[str, Any]
    config_dir: Path
    agencies_by_id: dict[str, dict[str, Any]] = field(default_factory=dict)
    people_by_id: dict[str, dict[str, Any]] = field(default_factory=dict)
    organizations_by_id: dict[str, dict[str, Any]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.agencies_by_id = {a["id"]: a for a in self.agencies}
        self.people_by_id = {p["id"]: p for p in self.people}
        self.organizations_by_id = {o["id"]: o for o in self.organizations}


def _load_yaml(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    return data


def _require(item: dict[str, Any], keys: list[str], label: str) -> None:
    missing = [key for key in keys if key not in item]
    if missing:
        raise ValueError(f"{label} is missing {missing}")


def load_registry(config_dir: str | Path) -> Registry:
    root = Path(config_dir)
    agencies = _load_yaml(root / "agencies.yaml") or []
    people = _load_yaml(root / "people.yaml") or []
    organizations = _load_yaml(root / "organizations.yaml") or []
    sources = _load_yaml(root / "sources.yaml") or []
    keywords = _load_yaml(root / "keywords.yaml") or {}
    _validate(agencies, people, organizations, sources, keywords)
    return Registry(
        agencies=agencies,
        people=people,
        organizations=organizations,
        sources=sources,
        keywords=keywords,
        config_dir=root,
    )


def _validate(
    agencies: list[dict[str, Any]],
    people: list[dict[str, Any]],
    organizations: list[dict[str, Any]],
    sources: list[dict[str, Any]],
    keywords: dict[str, Any],
) -> None:
    if not isinstance(agencies, list) or not agencies:
        raise ValueError("agencies.yaml must be a non-empty list")
    ids: set[str] = set()
    for agency in agencies:
        _require(
            agency,
            ["id", "name", "state", "type", "sworn_officers", "segment", "governing_body"],
            f"agency {agency.get('id')}",
        )
        if agency["id"] in ids:
            raise ValueError(f"duplicate agency id {agency['id']}")
        ids.add(agency["id"])
        if agency["type"] not in AGENCY_TYPES:
            raise ValueError(f"{agency['id']} has unknown type {agency['type']}")
        if not isinstance(agency["sworn_officers"], int):
            raise ValueError(f"{agency['id']} sworn_officers must be an integer")
        expected = segment_for_count(agency["sworn_officers"])
        if agency["segment"] != expected:
            raise ValueError(
                f"{agency['id']} segment {agency['segment']} does not match "
                f"sworn count {agency['sworn_officers']} ({expected})"
            )
        agency.setdefault("aliases", [])
        agency.setdefault("meeting_platform", None)
        agency.setdefault("meeting_url", None)
        agency.setdefault("youtube_channel", None)
        agency.setdefault("alpr_vendor", "unknown")
        agency.setdefault("legislative_districts", {})

    person_ids: set[str] = set()
    for person in people:
        _require(person, ["id", "name", "role", "body", "state", "agency_ids"], f"person {person.get('id')}")
        if person["id"] in person_ids:
            raise ValueError(f"duplicate person id {person['id']}")
        person_ids.add(person["id"])
        for agency_id in person.get("agency_ids") or []:
            if agency_id not in ids:
                raise ValueError(f"person {person['id']} links unknown agency {agency_id}")
        person.setdefault("on_governing_body", False)
        person.setdefault("source_urls", [])

    org_ids: set[str] = set()
    for org in organizations:
        _require(org, ["id", "name", "category"], f"organization {org.get('id')}")
        if org["id"] in org_ids:
            raise ValueError(f"duplicate organization id {org['id']}")
        org_ids.add(org["id"])

    known_entities = ids | person_ids | org_ids
    source_ids: set[str] = set()
    for source in sources:
        _require(source, ["id", "collector", "cadence"], f"source {source.get('id')}")
        if source["id"] in source_ids:
            raise ValueError(f"duplicate source id {source['id']}")
        source_ids.add(source["id"])
        if source["collector"] not in COLLECTORS:
            raise ValueError(f"source {source['id']} has unknown collector {source['collector']}")
        if source["cadence"] not in CADENCES:
            raise ValueError(f"source {source['id']} has unknown cadence {source['cadence']}")
        entity = source.get("entity")
        if entity and entity not in known_entities:
            raise ValueError(f"source {source['id']} links unknown entity {entity}")
        if source["collector"] in {"generic_html", "rss", "youtube", "primegov", "iqm2", "flock_transparency"}:
            if not source.get("url"):
                raise ValueError(f"source {source['id']} needs a url")
        if source["collector"] == "legiscan" and not source.get("state"):
            raise ValueError(f"source {source['id']} needs a state")

    strong = keywords.get("strong") or []
    weak = keywords.get("weak") or []
    if not strong or not weak:
        raise ValueError("keywords.yaml needs strong and weak lists")
