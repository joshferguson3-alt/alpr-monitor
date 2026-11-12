"""Print YAML for agency officials and state legislators. Never writes config/.

People and committee membership come from the Open States API when
OPENSTATES_API_KEY is set. Without the key, the script prints the review
template and the message "missing key, skipped".
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import httpx
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from monitor.constants import USER_AGENT  # noqa: E402
from monitor.registry import load_registry  # noqa: E402

COMMITTEE_HINTS = ("public safety", "judiciary", "science", "technology", "information")
JURISDICTIONS = {"GA": "Georgia", "TN": "Tennessee"}


def main() -> int:
    registry = load_registry(ROOT / "config")
    key = os.environ.get("OPENSTATES_API_KEY")
    people = [_leadership_stub(agency) for agency in registry.agencies]
    if not key:
        print("missing key, skipped (OPENSTATES_API_KEY)")
        print("# Review the YAML below. This script does not write config/.")
        print(yaml.safe_dump(people, sort_keys=False))
        return 0
    legislators = []
    with httpx.Client(timeout=20.0, headers={"User-Agent": USER_AGENT, "X-API-KEY": key}) as client:
        for state, jurisdiction in JURISDICTIONS.items():
            legislators.extend(_committees(client, jurisdiction, state))
    print("# Review the YAML below. This script does not write config/.")
    print(yaml.safe_dump(people + legislators, sort_keys=False))
    return 0


def _leadership_stub(agency: dict) -> dict:
    return {
        "id": f"{agency['id']}-leadership-review",
        "name": None,
        "role": "Chief or sheriff (fill from the official site)",
        "body": agency["name"],
        "state": agency["state"],
        "district": None,
        "agency_ids": [agency["id"]],
        "on_governing_body": False,
        "source_urls": [agency.get("meeting_url")] if agency.get("meeting_url") else [],
        "comment": "Placeholder for review. Do not commit a guessed name.",
    }


def _committees(client: httpx.Client, jurisdiction: str, state: str) -> list[dict]:
    response = client.get(
        "https://v3.openstates.org/committees",
        params={"jurisdiction": jurisdiction, "per_page": 50},
    )
    response.raise_for_status()
    payload = response.json()
    found = []
    for committee in payload.get("results") or []:
        name = committee.get("name") or ""
        if not any(hint in name.lower() for hint in COMMITTEE_HINTS):
            continue
        detail = client.get(f"https://v3.openstates.org/committees/{committee['id']}")
        detail.raise_for_status()
        body = detail.json()
        for member in body.get("memberships") or body.get("members") or []:
            person = member.get("person") or member
            person_name = person.get("name")
            if not person_name:
                continue
            found.append(
                {
                    "id": _slug(state, person_name),
                    "name": person_name,
                    "role": member.get("role") or "member",
                    "body": name,
                    "state": state,
                    "district": (person.get("current_role") or {}).get("district"),
                    "agency_ids": [],
                    "on_governing_body": False,
                    "source_urls": [f"https://openstates.org/person/{person.get('id')}/"] if person.get("id") else [],
                }
            )
    return found


def _slug(state: str, name: str) -> str:
    cleaned = "".join(ch.lower() if ch.isalnum() else "-" for ch in name)
    while "--" in cleaned:
        cleaned = cleaned.replace("--", "-")
    return f"{state.lower()}-{cleaned.strip('-')}"


if __name__ == "__main__":
    raise SystemExit(main())
