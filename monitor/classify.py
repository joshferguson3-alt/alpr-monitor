"""Send one passage to Claude Haiku and require a JSON object."""

from __future__ import annotations

import json
import re
from typing import Any

from monitor.constants import MODEL_ID, SIGNAL_TYPES, STANCE_TARGETS, STANCES

SYSTEM_PROMPT = """You extract structured facts about automated license plate readers (ALPR) and
police surveillance cameras from public government and news text.
Return one JSON object and nothing else. Use only what the passage states.
If a field is not stated, use null. Never guess names, dates, or amounts.
If the passage is not about ALPR or police surveillance cameras, return
{"is_relevant": false}."""

USER_TEMPLATE = """Source type: {doc_type}
Source title: {title}
Published: {published_at}
Known agency for this source (may be null): {agency_name}

Passage:
\"\"\"
{passage}
\"\"\"

Return JSON with these fields:
is_relevant        boolean
signal_type        one of: contract_event, budget_event, policy_event,
                   legislation_event, stance_statement, public_sentiment,
                   incident, vendor_move, leadership_change, funding_event
summary            one sentence, at most 40 words
agency_name        the law enforcement agency concerned, or null
vendors            list of vendor names mentioned
speaker_name       the official or spokesperson speaking, or null.
                   For private residents use null.
speaker_role       title and body, or "resident" for public comment
organization       organization the speaker represents, or null
stance             one of: supports, opposes, conditional, neutral, unclear
stance_target      one of: alpr_generally, incumbent_vendor, specific_vendor,
                   specific_policy
resident_counts    {{"support": n, "oppose": n}} for public comment, else null
key_date           ISO date of the vote, deadline, hearing, or contract end
key_date_meaning   short label for key_date
dollar_amount      number or null
camera_count       number or null
bill_id            bill number if any
quote              the single most informative sentence, copied exactly,
                   at most 300 characters
confidence         0.0 to 1.0

If the passage is not relevant, set is_relevant to false and set every other field to null.
"""

SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "is_relevant": {"type": "boolean"},
        "signal_type": {"type": ["string", "null"], "enum": [*SIGNAL_TYPES, None]},
        "summary": {"type": ["string", "null"]},
        "agency_name": {"type": ["string", "null"]},
        "vendors": {"type": ["array", "null"], "items": {"type": "string"}},
        "speaker_name": {"type": ["string", "null"]},
        "speaker_role": {"type": ["string", "null"]},
        "organization": {"type": ["string", "null"]},
        "stance": {"type": ["string", "null"], "enum": [*STANCES, None]},
        "stance_target": {"type": ["string", "null"], "enum": [*STANCE_TARGETS, None]},
        "resident_counts": {
            "type": ["object", "null"],
            "additionalProperties": False,
            "properties": {
                "support": {"type": "integer"},
                "oppose": {"type": "integer"},
            },
            "required": ["support", "oppose"],
        },
        "key_date": {"type": ["string", "null"]},
        "key_date_meaning": {"type": ["string", "null"]},
        "dollar_amount": {"type": ["number", "null"]},
        "camera_count": {"type": ["integer", "null"]},
        "bill_id": {"type": ["string", "null"]},
        "quote": {"type": ["string", "null"]},
        "confidence": {"type": ["number", "null"]},
    },
    "required": [
        "is_relevant",
        "signal_type",
        "summary",
        "agency_name",
        "vendors",
        "speaker_name",
        "speaker_role",
        "organization",
        "stance",
        "stance_target",
        "resident_counts",
        "key_date",
        "key_date_meaning",
        "dollar_amount",
        "camera_count",
        "bill_id",
        "quote",
        "confidence",
    ],
}


def build_user_message(*, doc_type: str, title: str, published_at: str | None, agency_name: str | None, passage: str) -> str:
    return USER_TEMPLATE.format(
        doc_type=doc_type or "",
        title=title or "",
        published_at=published_at or "",
        agency_name=agency_name or "null",
        passage=passage,
    )


def _text_from_response(response: Any) -> str:
    parts: list[str] = []
    for block in getattr(response, "content", []) or []:
        if getattr(block, "type", None) == "text" and getattr(block, "text", None):
            parts.append(block.text)
        elif isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text") or "")
    return "\n".join(parts).strip()


def parse_model_json(raw: str) -> dict[str, Any]:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?", "", text).strip()
        text = re.sub(r"```$", "", text).strip()
    data = json.loads(text)
    if not isinstance(data, dict) or "is_relevant" not in data:
        raise ValueError("classifier JSON missing is_relevant")
    return data


def _normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def quote_is_in_passage(quote: str | None, passage: str) -> bool:
    if not quote:
        return False
    return _normalize_space(quote) in _normalize_space(passage)


def apply_privacy(result: dict[str, Any]) -> dict[str, Any]:
    """Residents are counts and stance only. Never store their names."""
    role = (result.get("speaker_role") or "").lower()
    is_resident = "resident" in role or result.get("signal_type") == "public_sentiment"
    if is_resident and result.get("speaker_name"):
        name = result["speaker_name"]
        result["speaker_name"] = None
        for field in ("summary", "quote"):
            if result.get(field) and name in result[field]:
                result[field] = result[field].replace(name, "a resident")
        result["needs_review"] = 1
    if result.get("signal_type") == "public_sentiment":
        result["speaker_name"] = None
    return result


def finalize(result: dict[str, Any], passage: str) -> dict[str, Any]:
    result = apply_privacy(result)
    result.setdefault("needs_review", 0)
    if result.get("is_relevant"):
        if not quote_is_in_passage(result.get("quote"), passage):
            result["needs_review"] = 1
        if result.get("signal_type") not in SIGNAL_TYPES:
            result["needs_review"] = 1
    return result


class Classifier:
    def __init__(self, client: Any | None = None, model: str = MODEL_ID):
        self.model = model
        self._client = client

    @property
    def client(self):
        if self._client is None:
            import os

            if not os.environ.get("ANTHROPIC_API_KEY"):
                raise RuntimeError("ANTHROPIC_API_KEY is not set")
            from anthropic import Anthropic

            self._client = Anthropic()
        return self._client

    def classify(
        self,
        *,
        passage: str,
        doc_type: str,
        title: str,
        published_at: str | None,
        agency_name: str | None,
    ) -> dict[str, Any]:
        user = build_user_message(
            doc_type=doc_type,
            title=title,
            published_at=published_at,
            agency_name=agency_name,
            passage=passage,
        )
        last_error: Exception | None = None
        for _attempt in range(2):
            response = self.client.messages.create(
                model=self.model,
                max_tokens=1024,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user}],
                output_config={
                    "effort": "low",
                    "format": {"type": "json_schema", "schema": SCHEMA},
                },
            )
            raw = _text_from_response(response)
            try:
                parsed = parse_model_json(raw)
            except (json.JSONDecodeError, ValueError) as exc:
                last_error = exc
                continue
            return finalize(parsed, passage)
        raise ValueError(f"classifier response did not parse: {last_error}")
