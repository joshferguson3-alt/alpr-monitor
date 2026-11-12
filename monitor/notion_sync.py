"""Upsert signals and agencies into Notion. object_not_found is reported, not fatal."""

from __future__ import annotations

import os
import re
from datetime import date
from typing import Any

from monitor.constants import NOTION_VERSION, SIGNAL_TYPES
from monitor.score import account_temperature

SIGNAL_PROPERTIES = {
    "Summary": {"title": {}},
    "Tier": {"select": {"options": [{"name": "Act now"}, {"name": "Watch"}, {"name": "Background"}]}},
    "Score": {"number": {"format": "number"}},
    "Type": {"select": {"options": [{"name": name} for name in SIGNAL_TYPES]}},
    "Agency": {"select": {"options": []}},
    "State": {
        "select": {
            "options": [{"name": code} for code in ("GA", "TN", "AL", "FL", "SC", "NC", "US")]
        }
    },
    "Speaker": {"rich_text": {}},
    "Role": {"rich_text": {}},
    "Stance": {
        "select": {
            "options": [{"name": name} for name in ("supports", "opposes", "conditional", "neutral", "unclear")]
        }
    },
    "Vendors": {"multi_select": {"options": []}},
    "Key date": {"date": {}},
    "Key date meaning": {"rich_text": {}},
    "Amount": {"number": {"format": "dollar"}},
    "Cameras": {"number": {"format": "number"}},
    "Bill": {"rich_text": {}},
    "Quote": {"rich_text": {}},
    "Source": {"url": {}},
    "Published": {"date": {}},
    "Needs review": {"checkbox": {}},
    "Signal ID": {"number": {"format": "number"}},
}

AGENCY_PROPERTIES = {
    "Name": {"title": {}},
    "Agency ID": {"rich_text": {}},
    "State": {"select": {"options": [{"name": code} for code in ("GA", "TN", "AL", "FL", "SC", "NC")]}},
    "Segment": {
        "select": {
            "options": [{"name": name} for name in ("inside", "mid_city", "t1200", "major")]
        }
    },
    "Sworn officers": {"number": {"format": "number"}},
    "ALPR vendor": {
        "select": {
            "options": [{"name": name} for name in ("flock", "axon", "motorola", "other", "none", "unknown")]
        }
    },
    "Camera count": {"number": {"format": "number"}},
    "Contract end date": {"date": {}},
    "Account temperature": {"number": {"format": "number"}},
    "Last signal date": {"date": {}},
}


def is_object_not_found(exc: Exception) -> bool:
    code = getattr(exc, "code", None)
    if code == "object_not_found":
        return True
    return "object_not_found" in str(exc)


def _rich(value: str | None) -> dict | None:
    if not value:
        return None
    return {"rich_text": [{"type": "text", "text": {"content": value[:1900]}}]}


def _title(value: str) -> dict:
    return {"title": [{"type": "text", "text": {"content": (value or "Signal")[:1900]}}]}


def sanitize_option(value: str | None, limit: int = 100) -> str | None:
    """Notion select names cannot contain commas and must be at most 100 characters."""
    if value is None:
        return None
    text = str(value).replace(",", " -")
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return None
    return text[:limit]


def _select(value: str | None) -> dict | None:
    name = sanitize_option(value)
    if not name:
        return None
    return {"select": {"name": name}}


def _date(value: str | None) -> dict | None:
    if not value:
        return None
    return {"date": {"start": value[:10]}}


class NotionSync:
    def __init__(self, client: Any | None = None, signals_db: str | None = None, agencies_db: str | None = None):
        self.client = client
        self.signals_db = signals_db or os.environ.get("NOTION_SIGNALS_DB_ID") or ""
        self.agencies_db = agencies_db or os.environ.get("NOTION_AGENCIES_DB_ID") or ""
        self._signals_ds: str | None = None
        self._agencies_ds: str | None = None
        self.notes: list[str] = []

    def enabled(self) -> bool:
        return bool(self.client and (self.signals_db or self.agencies_db))

    def _data_source_id(self, database_id: str) -> str:
        try:
            database = self.client.databases.retrieve(database_id=database_id)
        except Exception as exc:
            if is_object_not_found(exc):
                self.notes.append(
                    f"Notion returned object_not_found for database {database_id}. "
                    "The integration may not have access to the page yet."
                )
                return ""
            raise
        sources = database.get("data_sources") or []
        if not sources:
            # Older responses put the id on the database itself.
            return database_id
        return sources[0]["id"]

    def signals_data_source(self) -> str:
        if self._signals_ds is None:
            self._signals_ds = self._data_source_id(self.signals_db) if self.signals_db else ""
        return self._signals_ds

    def agencies_data_source(self) -> str:
        if self._agencies_ds is None:
            self._agencies_ds = self._data_source_id(self.agencies_db) if self.agencies_db else ""
        return self._agencies_ds

    def upsert_signal(self, signal: dict, document: dict, agency: dict | None) -> str | None:
        data_source = self.signals_data_source()
        if not data_source:
            return None
        properties = self._signal_properties(signal, document, agency)
        existing = self._find_number(data_source, "Signal ID", int(signal["id"]))
        try:
            if existing:
                page = self.client.pages.update(page_id=existing, properties=properties)
            else:
                page = self.client.pages.create(
                    parent={"type": "data_source_id", "data_source_id": data_source},
                    properties=properties,
                )
        except Exception as exc:
            self.notes.append(str(exc)[:500])
            return None
        return page.get("id")

    def upsert_agencies(self, agencies: list[dict], signals: list[dict], today: date) -> None:
        data_source = self.agencies_data_source()
        if not data_source:
            return
        by_agency: dict[str, list[dict]] = {}
        for signal in signals:
            if signal.get("agency_id"):
                by_agency.setdefault(signal["agency_id"], []).append(signal)
        for agency in agencies:
            rows = by_agency.get(agency["id"], [])
            last = None
            for signal in rows:
                created = (signal.get("created_at") or "")[:10]
                if created and (last is None or created > last):
                    last = created
            properties = {
                "Name": _title(agency["name"]),
                "Agency ID": _rich(agency["id"]),
                "State": _select(agency.get("state")),
                "Segment": _select(agency.get("segment")),
                "Sworn officers": {"number": agency.get("sworn_officers")},
                "ALPR vendor": _select(agency.get("alpr_vendor") or "unknown"),
                "Account temperature": {"number": account_temperature(rows, today=today)},
            }
            if agency.get("alpr_camera_count") is not None:
                properties["Camera count"] = {"number": agency["alpr_camera_count"]}
            if agency.get("contract_end_date"):
                properties["Contract end date"] = _date(agency["contract_end_date"])
            if last:
                properties["Last signal date"] = _date(last)
            existing = self._find_text(data_source, "Agency ID", agency["id"])
            try:
                if existing:
                    self.client.pages.update(page_id=existing, properties=properties)
                else:
                    self.client.pages.create(
                        parent={"type": "data_source_id", "data_source_id": data_source},
                        properties=properties,
                    )
            except Exception as exc:
                self.notes.append(str(exc)[:500])
                if is_object_not_found(exc):
                    return
                continue

    def _signal_properties(self, signal: dict, document: dict, agency: dict | None) -> dict:
        vendors = signal.get("vendors") or []
        if isinstance(vendors, str):
            import json
            vendors = json.loads(vendors)
        properties = {
            "Summary": _title(signal.get("summary") or "Signal"),
            "Tier": _select(signal.get("tier")),
            "Score": {"number": signal.get("score")},
            "Type": _select(signal.get("signal_type")),
            "Speaker": _rich(signal.get("speaker_name_raw")),
            "Role": _rich(signal.get("speaker_role")),
            "Stance": _select(signal.get("stance")),
            "Key date meaning": _rich(signal.get("key_date_meaning")),
            "Bill": _rich(signal.get("bill_id")),
            "Quote": _rich(signal.get("quote")),
            "Source": {"url": document.get("url")} if document.get("url") else None,
            "Key date": _date(signal.get("key_date")),
            "Published": _date(document.get("published_at")),
            "Needs review": {"checkbox": bool(signal.get("needs_review"))},
            "Signal ID": {"number": int(signal["id"])},
        }
        if signal.get("dollar_amount") is not None:
            properties["Amount"] = {"number": signal["dollar_amount"]}
        if signal.get("camera_count") is not None:
            properties["Cameras"] = {"number": signal["camera_count"]}
        if agency:
            properties["Agency"] = _select(agency["name"])
            properties["State"] = _select(agency.get("state"))
        elif signal.get("agency_name_raw"):
            properties["Agency"] = _select(signal["agency_name_raw"])
        if vendors:
            names = []
            for vendor in vendors:
                name = sanitize_option(vendor)
                if name and name not in names:
                    names.append(name)
            if names:
                properties["Vendors"] = {"multi_select": [{"name": name} for name in names]}
        return {key: value for key, value in properties.items() if value is not None}

    def _find_number(self, data_source_id: str, prop: str, value: int) -> str | None:
        result = self.client.data_sources.query(
            data_source_id=data_source_id,
            filter={"property": prop, "number": {"equals": value}},
            page_size=1,
        )
        rows = result.get("results") or []
        return rows[0]["id"] if rows else None

    def _find_text(self, data_source_id: str, prop: str, value: str) -> str | None:
        result = self.client.data_sources.query(
            data_source_id=data_source_id,
            filter={"property": prop, "rich_text": {"equals": value}},
            page_size=1,
        )
        rows = result.get("results") or []
        return rows[0]["id"] if rows else None


def client_from_env():
    token = os.environ.get("NOTION_TOKEN")
    if not token:
        return None
    from notion_client import Client

    return Client(auth=token, notion_version=NOTION_VERSION)
