"""Collector interface."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Protocol

from monitor.http import HttpClient


class MissingKeyError(RuntimeError):
    """Raised when a collector's API key is not in the environment."""

    def __init__(self, env_name: str):
        super().__init__(f"missing key, skipped ({env_name})")
        self.env_name = env_name


@dataclass
class RawDocument:
    source_id: str
    url: str
    title: str
    published_at: str | None
    doc_type: str
    text: str
    # A search hit whose full text could not be fetched still goes to classification.
    force_prefilter: bool = False

    def as_dict(self) -> dict:
        return {
            "source_id": self.source_id,
            "url": self.url,
            "title": self.title,
            "published_at": self.published_at,
            "doc_type": self.doc_type,
            "text": self.text,
            "force_prefilter": self.force_prefilter,
        }


class Collector(Protocol):
    def collect(
        self,
        source: dict,
        *,
        since: date,
        until: date,
        http: HttpClient,
    ) -> list[RawDocument]: ...
