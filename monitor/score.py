"""Priority score from the spec's base values and modifiers."""

from __future__ import annotations

from datetime import date, datetime

from monitor.bills import is_local_appropriation, is_program_or_ban, status_kind
from monitor.constants import COVERED_STATES, INCUMBENT_VENDOR

BASE = {
    "contract_event": 60,
    "leadership_change": 50,
    "incident": 50,
    "policy_event": 50,
    "budget_event": 45,
    "funding_event": 45,
    "legislation_event": 40,
    "vendor_move": 35,
    "stance_statement": 30,
    "public_sentiment": 25,
}


def tier_for(score: int) -> str:
    if score >= 70:
        return "Act now"
    if score >= 40:
        return "Watch"
    return "Background"


def _round_cap(value: float) -> int:
    if value <= 0:
        return 0
    return min(100, int(value + 0.5))


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value[:10]).date()
    except ValueError:
        return None


def _text(signal: dict) -> str:
    return " ".join(
        str(signal.get(key) or "")
        for key in ("summary", "quote", "key_date_meaning")
    ).lower()


def _names_incumbent(signal: dict) -> bool:
    vendors = signal.get("vendors") or []
    if any(INCUMBENT_VENDOR in str(vendor).lower() for vendor in vendors):
        return True
    return "flock" in _text(signal)


def _is_incumbent_vendor(agency: dict | None) -> bool:
    if not agency:
        return False
    return str(agency.get("alpr_vendor") or "").lower() == INCUMBENT_VENDOR


def score_signal(
    signal: dict,
    *,
    agency: dict | None,
    person: dict | None,
    stance_changed: bool,
    today: date,
    state: str | None = None,
) -> tuple[int, str]:
    signal_type = signal["signal_type"]
    base = BASE[signal_type]
    modifiers = 0
    at_registry = agency is not None
    blob = _text(signal)

    if signal_type == "contract_event":
        if _is_incumbent_vendor(agency):
            modifiers += 20
        key = _parse_date(signal.get("key_date"))
        if key is not None and abs((key - today).days) <= 180:
            modifiers += 15
        if any(phrase in blob for phrase in ("termination", "non-renewal", "nonrenewal", "not renew", "terminate")):
            modifiers += 10
    elif signal_type == "leadership_change":
        if at_registry:
            modifiers += 10
    elif signal_type == "incident":
        if at_registry:
            modifiers += 20
        if _names_incumbent(signal):
            modifiers += 10
    elif signal_type == "policy_event":
        if at_registry:
            modifiers += 15
    elif signal_type == "budget_event":
        if signal.get("dollar_amount") not in (None, ""):
            modifiers += 15
    elif signal_type == "funding_event":
        if at_registry:
            modifiers += 10
    elif signal_type == "legislation_event":
        kind = status_kind(blob)
        local = is_local_appropriation(blob)
        if local:
            modifiers -= 20
        else:
            if "hearing" in blob:
                modifiers += 20
            if kind == "passed":
                modifiers += 35
            elif kind == "enacted":
                modifiers += 50
            elif kind == "dead":
                modifiers -= 25
    elif signal_type == "vendor_move":
        covered = (state or (agency or {}).get("state") or "").upper()
        if covered in COVERED_STATES:
            modifiers += 20
    elif signal_type == "stance_statement":
        on_body = bool(person and person.get("on_governing_body"))
        if on_body and at_registry:
            modifiers += 20
        if stance_changed:
            modifiers += 15
    elif signal_type == "public_sentiment":
        support = int(signal.get("resident_support") or 0)
        oppose = int(signal.get("resident_oppose") or 0)
        if support + oppose >= 5:
            modifiers += 15
        if at_registry:
            modifiers += 10

    confidence = float(signal.get("confidence") if signal.get("confidence") is not None else 1.0)
    score = _round_cap((base + modifiers) * confidence)
    if signal_type == "legislation_event" and not is_local_appropriation(blob):
        kind = status_kind(blob)
        covered = (state or (agency or {}).get("state") or "").upper()
        if not covered or covered in COVERED_STATES:
            if kind == "enacted" and is_program_or_ban(blob):
                score = max(score, 70)
            elif kind == "enacted":
                score = max(score, 48)
            elif kind == "passed":
                score = max(score, 42)
    if signal_type == "legislation_event" and is_local_appropriation(blob):
        score = min(score, 35)
    return score, tier_for(score)


def full_weight(signal: dict) -> bool:
    """Contract events and opposition to the incumbent count at full weight."""
    if signal.get("signal_type") == "contract_event":
        return True
    return signal.get("stance") == "opposes" and signal.get("stance_target") == "incumbent_vendor"


def account_temperature(signals: list[dict], *, today: date, window_days: int = 90) -> float:
    total = 0.0
    for signal in signals:
        created = _parse_date(signal.get("created_at"))
        if created is None:
            continue
        age = (today - created).days
        if age < 0 or age > window_days:
            continue
        weight = 1.0 if full_weight(signal) else 0.5
        total += float(signal.get("score") or 0) * weight
    return round(total, 1)
