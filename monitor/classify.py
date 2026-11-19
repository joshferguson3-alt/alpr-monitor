"""Send one passage to Claude Haiku and require a JSON object."""

from __future__ import annotations

import json
import re
from typing import Any

from monitor.constants import MODEL_ID, SIGNAL_TYPES, STANCE_TARGETS, STANCES

SYSTEM_PROMPT = """You extract structured facts about automated license plate readers (ALPR) and
police surveillance cameras from public government and news text.
Return one JSON object and nothing else. Use only what the passage states.
If a field is not stated, use an empty string, "none", -1, or an empty list.
Never guess names, dates, or amounts.
If the passage is not about ALPR or police surveillance cameras, set
is_relevant to false and use those empty values for every other field.
Judge relevance against the source title. If the title is about a different
subject and the ALPR mention is only a related-links list, a sidebar, a
navigation menu, a footer, or an author bio, set is_relevant to false.
Exception for source types agenda, minutes, and packet: a council or commission
item can concern these systems without naming them. If the item is a police or
public safety technology, camera, or vendor contract, purchase, renewal, budget
line, or policy that could plausibly cover license plate readers or police
cameras, set is_relevant to true, begin the summary with "Possible ALPR item:",
say what the item covers and that the product is not named, and set confidence
to 0.5 or lower. Items that are clearly something else, such as body armor,
vehicles, radios, or software unrelated to cameras, are not relevant."""

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
                   incident, vendor_move, leadership_change, funding_event, none
summary            one sentence, at most 40 words
agency_name        the law enforcement agency concerned, or ""
vendors            list of vendor names mentioned, or []
speaker_name       the official or spokesperson speaking, or "".
                   For private residents use "".
speaker_role       title and body, "resident" for public comment, or ""
organization       organization the speaker represents, or ""
stance             one of: supports, opposes, conditional, neutral, unclear, none
stance_target      one of: alpr_generally, incumbent_vendor, specific_vendor,
                   specific_policy, none
resident_counts    {{"support": n, "oppose": n}} for public comment.
                   Use -1 for both when there is no public comment.
key_date           ISO date of the vote, deadline, hearing, or contract end, or ""
key_date_meaning   short label for key_date, or ""
dollar_amount      number, or -1 when no amount is stated
camera_count       number, or -1 when no count is stated
bill_id            bill number if any, or ""
quote              the single most informative sentence, copied exactly,
                   at most 300 characters, or ""
confidence         0.0 to 1.0

If the passage is not relevant, set is_relevant to false and use the empty
sentinels above. Do not use null.
"""

SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "is_relevant": {"type": "boolean"},
        "signal_type": {"type": "string", "enum": [*SIGNAL_TYPES, "none"]},
        "summary": {"type": "string"},
        "agency_name": {"type": "string"},
        "vendors": {"type": "array", "items": {"type": "string"}},
        "speaker_name": {"type": "string"},
        "speaker_role": {"type": "string"},
        "organization": {"type": "string"},
        "stance": {"type": "string", "enum": [*STANCES, "none"]},
        "stance_target": {"type": "string", "enum": [*STANCE_TARGETS, "none"]},
        "resident_counts": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "support": {"type": "integer"},
                "oppose": {"type": "integer"},
            },
            "required": ["support", "oppose"],
        },
        "key_date": {"type": "string"},
        "key_date_meaning": {"type": "string"},
        "dollar_amount": {"type": "number"},
        "camera_count": {"type": "integer"},
        "bill_id": {"type": "string"},
        "quote": {"type": "string"},
        "confidence": {"type": "number"},
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


def _blank_to_none(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, str) and value.strip().lower() in {"", "none", "null"}:
        return None
    return value


def normalize_sentinels(data: dict[str, Any]) -> dict[str, Any]:
    """Turn empty-string, "none", -1, and empty-list sentinels back into None."""
    for key in (
        "summary",
        "agency_name",
        "speaker_name",
        "speaker_role",
        "organization",
        "key_date",
        "key_date_meaning",
        "bill_id",
        "quote",
        "signal_type",
        "stance",
        "stance_target",
    ):
        if key in data:
            data[key] = _blank_to_none(data.get(key))
    vendors = data.get("vendors")
    if not vendors:
        data["vendors"] = None
    else:
        data["vendors"] = [item for item in vendors if _blank_to_none(item)]
        if not data["vendors"]:
            data["vendors"] = None
    counts = data.get("resident_counts")
    if not isinstance(counts, dict):
        data["resident_counts"] = None
    else:
        support = counts.get("support", -1)
        oppose = counts.get("oppose", -1)
        if (support is None or support < 0) and (oppose is None or oppose < 0):
            data["resident_counts"] = None
        else:
            data["resident_counts"] = {
                "support": 0 if support is None or support < 0 else support,
                "oppose": 0 if oppose is None or oppose < 0 else oppose,
            }
    for key in ("dollar_amount", "camera_count"):
        value = data.get(key)
        if value is None or value < 0:
            data[key] = None
    if data.get("confidence") is None:
        data["confidence"] = 0
    return data


def parse_model_json(raw: str) -> dict[str, Any]:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?", "", text).strip()
        text = re.sub(r"```$", "", text).strip()
    data = json.loads(text)
    if not isinstance(data, dict) or "is_relevant" not in data:
        raise ValueError("classifier JSON missing is_relevant")
    return normalize_sentinels(data)


def _normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def quote_is_in_passage(quote: str | None, passage: str) -> bool:
    if not quote:
        return False
    return _normalize_space(quote) in _normalize_space(passage)


def apply_privacy(result: dict[str, Any]) -> dict[str, Any]:
    """Residents are counts and stance only. Never store their names."""
    role = (result.get("speaker_role") or "").strip().lower()
    role_is_resident = bool(re.search(r"\b(resident|citizen|public comment(er)?|member of the public)\b", role))
    is_resident = role_is_resident or (result.get("signal_type") == "public_sentiment" and not role)
    if is_resident and result.get("speaker_name"):
        name = result["speaker_name"]
        result["speaker_name"] = None
        for field in ("summary", "quote"):
            if result.get(field) and name in result[field]:
                result[field] = result[field].replace(name, "a resident")
        result["needs_review"] = 1
    return result


def finalize(result: dict[str, Any], passage: str) -> dict[str, Any]:
    result = normalize_sentinels(dict(result))
    result = apply_privacy(result)
    summary = result.get("summary") or ""
    if summary.startswith("Possible ALPR item:"):
        confidence = result.get("confidence")
        if confidence is None or confidence > 0.5:
            result["confidence"] = 0.5
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
