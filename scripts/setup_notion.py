"""Create the Signals and Agencies databases under NOTION_PARENT_PAGE_ID.

Prints NOTION_SIGNALS_DB_ID and NOTION_AGENCIES_DB_ID. If Notion returns
object_not_found, the integration has not been shared with the parent page.
That case is reported and the process exits 0 so the build can continue.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from monitor.constants import NOTION_VERSION  # noqa: E402
from monitor.notion_sync import AGENCY_PROPERTIES, SIGNAL_PROPERTIES, is_object_not_found  # noqa: E402
from monitor.registry import load_registry  # noqa: E402


def main() -> int:
    token = os.environ.get("NOTION_TOKEN")
    parent = os.environ.get("NOTION_PARENT_PAGE_ID")
    if not token or not parent:
        print("NOTION_TOKEN and NOTION_PARENT_PAGE_ID are required.")
        return 1
    from notion_client import Client

    registry = load_registry(ROOT / "config")
    properties = dict(SIGNAL_PROPERTIES)
    properties["Agency"] = {
        "select": {"options": [{"name": agency["name"]} for agency in registry.agencies]}
    }
    vendor_names = sorted(
        {
            *(agency.get("alpr_vendor") or "unknown" for agency in registry.agencies),
            "Flock Safety",
            "Axon",
            "Motorola Solutions",
            "Vigilant",
            "Rekor",
            "Fusus",
        }
    )
    properties["Vendors"] = {"multi_select": {"options": [{"name": name} for name in vendor_names]}}
    notion = Client(auth=token, notion_version=NOTION_VERSION)
    try:
        signals = notion.databases.create(
            parent={"type": "page_id", "page_id": parent},
            title=[{"type": "text", "text": {"content": "Signals"}}],
            initial_data_source={"properties": properties},
        )
        agencies = notion.databases.create(
            parent={"type": "page_id", "page_id": parent},
            title=[{"type": "text", "text": {"content": "Agencies"}}],
            initial_data_source={"properties": AGENCY_PROPERTIES},
        )
    except Exception as exc:
        if is_object_not_found(exc):
            print("Notion returned object_not_found.")
            print("The integration does not have access to the parent page yet.")
            print(f"Parent page id: {parent}")
            print("Share that page with the integration, then run this script again.")
            return 0
        raise

    signals_id = signals["id"]
    agencies_id = agencies["id"]
    print(f"NOTION_SIGNALS_DB_ID={signals_id}")
    print(f"NOTION_AGENCIES_DB_ID={agencies_id}")
    _create_views(notion, signals)
    return 0


def _data_source_id(database: dict) -> str:
    sources = database.get("data_sources") or []
    if sources:
        return sources[0]["id"]
    return database["id"]


def _property_id(notion, data_source_id: str, name: str) -> str | None:
    source = notion.data_sources.retrieve(data_source_id=data_source_id)
    prop = (source.get("properties") or {}).get(name) or {}
    return prop.get("id")


def _create_views(notion, signals_db: dict) -> None:
    database_id = signals_db["id"]
    data_source_id = _data_source_id(signals_db)
    try:
        agency_id = _property_id(notion, data_source_id, "Agency")
        state_id = _property_id(notion, data_source_id, "State")
        date_id = _property_id(notion, data_source_id, "Key date")
        if agency_id:
            notion.views.create(
                database_id=database_id,
                data_source_id=data_source_id,
                name="Act now",
                type="board",
                filter={"property": "Tier", "select": {"equals": "Act now"}},
                configuration={
                    "type": "board",
                    "group_by": {
                        "type": "select",
                        "property_id": agency_id,
                        "sort": {"type": "ascending"},
                    },
                },
            )
        if date_id:
            notion.views.create(
                database_id=database_id,
                data_source_id=data_source_id,
                name="Upcoming dates",
                type="calendar",
                configuration={"type": "calendar", "date_property_id": date_id},
            )
        if state_id:
            notion.views.create(
                database_id=database_id,
                data_source_id=data_source_id,
                name="By state",
                type="table",
                configuration={
                    "type": "table",
                    "group_by": {
                        "type": "select",
                        "property_id": state_id,
                        "sort": {"type": "ascending"},
                    },
                },
            )
        print("Created views: Act now, Upcoming dates, By state")
    except Exception as exc:
        if is_object_not_found(exc):
            print("Notion returned object_not_found while creating views.")
            return
        print(f"Databases were created. View creation failed: {exc}")


if __name__ == "__main__":
    raise SystemExit(main())
