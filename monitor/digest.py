"""Plain-text daily digest. Sent only to the configured recipient."""

from __future__ import annotations

import smtplib
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from typing import Any, Callable

from monitor.constants import DIGEST_RECIPIENT
from monitor.score import account_temperature

SendFunc = Callable[[str, str, str], None]


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value[:10]).date()
    except ValueError:
        return None


def render_digest(
    signals: list[dict[str, Any]],
    *,
    documents: dict[int, dict[str, Any]],
    agencies: dict[str, dict[str, Any]],
    run_errors: list[dict[str, str]],
    sources_checked: int,
    today: date,
    weekly: bool = False,
    recent_signals: list[dict[str, Any]] | None = None,
    lookback_days: int | None = None,
) -> tuple[str, str] | None:
    """Return (subject, body) when at least one signal scores 40 or higher."""
    visible = [signal for signal in signals if (signal.get("score") or 0) >= 20]
    act = [signal for signal in visible if (signal.get("score") or 0) >= 70]
    watch = [signal for signal in visible if 40 <= (signal.get("score") or 0) <= 69]
    if not act and not watch:
        return None

    def line(index: int, signal: dict[str, Any]) -> str:
        agency = agencies.get(signal.get("agency_id") or "", {})
        label = agency.get("name") or signal.get("agency_name_raw") or "Unassigned"
        short = label.replace(" Police Department", " PD")
        summary = signal.get("summary") or ""
        if signal.get("key_date"):
            summary = f"{summary} {signal.get('key_date_meaning') or 'Date'} {signal['key_date']}."
        quote = signal.get("quote") or ""
        doc = documents.get(signal.get("document_id") or -1, {})
        url = doc.get("url") or ""
        return (
            f"{index}. [{signal.get('score')}] {short}: {signal.get('signal_type')}\n"
            f"   {summary.strip()}\n"
            f"   \"{quote}\"\n"
            f"   {url}"
        )

    sections = ["ACT NOW"]
    if act:
        sections.extend(line(i, signal) for i, signal in enumerate(act, 1))
    else:
        sections.append("(none)")
    sections.append("")
    sections.append("WATCH")
    if watch:
        sections.extend(line(i, signal) for i, signal in enumerate(watch, 1))
    else:
        sections.append("(none)")

    upcoming = []
    horizon = today + timedelta(days=30)
    for signal in signals:
        key = _parse_date(signal.get("key_date"))
        if key and today <= key <= horizon:
            agency = agencies.get(signal.get("agency_id") or "", {})
            label = agency.get("governing_body") or agency.get("name") or signal.get("agency_name_raw") or ""
            meaning = signal.get("key_date_meaning") or signal.get("signal_type")
            upcoming.append((key.isoformat(), f"{key.isoformat()}  {label} {meaning}".strip()))
    sections.append("")
    sections.append("UPCOMING DATES (next 30 days)")
    sections.extend(item for _, item in sorted(upcoming) or [("", "(none)")])

    if weekly:
        sections.extend(_weekly_section(recent_signals or signals, agencies, today))

    failed = run_errors or []
    detail = "; ".join(f"{err.get('source_id')}: {err.get('message')}" for err in failed) or "none"
    sections.append("")
    sections.append(
        f"RUN HEALTH\n{sources_checked} sources checked, {len(failed)} failed ({detail})"
    )
    subject = f"ALPR signals: {len(act)} act now, {len(watch)} watch ({today.isoformat()})"
    if lookback_days:
        subject = f"{subject} Backfill: last {lookback_days} days"
    return subject, "\n".join(sections).strip() + "\n"


def _weekly_section(signals: list[dict[str, Any]], agencies: dict[str, dict[str, Any]], today: date) -> list[str]:
    lines = ["", "WEEKLY", "Account temperature (90-day weighted score)"]
    by_agency: dict[str, list[dict[str, Any]]] = {}
    for signal in signals:
        agency_id = signal.get("agency_id")
        if agency_id:
            by_agency.setdefault(agency_id, []).append(signal)
    ranked = []
    for agency_id, agency in agencies.items():
        temp = account_temperature(by_agency.get(agency_id, []), today=today)
        ranked.append((temp, agency.get("name") or agency_id))
    ranked.sort(reverse=True)
    if ranked:
        lines.extend(f"{temp:g}  {name}" for temp, name in ranked)
    else:
        lines.append("(none)")
    week_start = today - timedelta(days=7)
    bills = []
    shifts = []
    for signal in signals:
        created = _parse_date(signal.get("created_at"))
        if created and created < week_start:
            continue
        if signal.get("signal_type") == "legislation_event":
            bills.append(signal.get("summary") or signal.get("bill_id") or "bill")
        if str(signal.get("summary") or "").startswith("Stance shift:"):
            shifts.append(signal.get("summary"))
    lines.append("New bills")
    lines.extend(bills or ["(none)"])
    lines.append("Stance shifts")
    lines.extend(shifts or ["(none)"])
    return lines


def send_gmail(subject: str, body: str, *, user: str, password: str, recipient: str) -> None:
    if recipient.lower() != DIGEST_RECIPIENT:
        raise RuntimeError(f"refusing to email {recipient}; digest recipient is fixed")
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = user
    message["To"] = recipient
    message.set_content(body)
    with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.login(user, password)
        smtp.send_message(message)


def deliver(
    subject: str,
    body: str,
    *,
    smtp_user: str | None,
    smtp_password: str | None,
    recipient: str | None,
    sender: SendFunc | None = None,
) -> str:
    """Send the digest or return a skip reason. Never emails any address but the fixed recipient."""
    target = (recipient or "").strip()
    if target.lower() != DIGEST_RECIPIENT:
        return f"digest not sent: recipient must be {DIGEST_RECIPIENT}"
    if sender is not None:
        sender(subject, body, target)
        return "sent"
    if not smtp_user or not smtp_password:
        return "digest not sent: SMTP credentials are not set"
    send_gmail(subject, body, user=smtp_user, password=smtp_password, recipient=target)
    return "sent"
