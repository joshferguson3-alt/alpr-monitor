"""Live classifier check. Skips unless ANTHROPIC_API_KEY is set.

The hand-labeled fixture is tests/fixtures/passages.json: 5 relevant, 5 not.
A passing run gets at least 9 of 10 relevance labels right.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from monitor.classify import Classifier

FIXTURE = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "passages.json"


@pytest.mark.skipif(not os.environ.get("ANTHROPIC_API_KEY"), reason="ANTHROPIC_API_KEY is not set")
def test_haiku_labels_at_least_nine_of_ten():
    items = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert len(items) == 10
    assert sum(1 for item in items if item["relevant"]) == 5
    classifier = Classifier()
    correct = 0
    for item in items:
        result = classifier.classify(
            passage=item["text"],
            doc_type="news",
            title=item["id"],
            published_at="2026-11-01",
            agency_name=None,
        )
        if bool(result.get("is_relevant")) == bool(item["relevant"]):
            correct += 1
    assert correct >= 9
