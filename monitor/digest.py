"""Daily digest. Sent only to the configured recipient."""

from __future__ import annotations

import html
import re
import smtplib
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from typing import Any, Callable

from monitor.constants import DIGEST_RECIPIENT
from monitor.impact import MIXED, badge, derive_alpr_stance
from monitor.score import account_temperature

SendFunc = Callable[[str, str, str], None]


def source_health_lines(
    stats: dict[str, dict[str, int]],
    sources: list[dict[str, Any]],
    skipped: list[dict[str, str]],
    errors: list[dict[str, str]],
    prior_counts: dict[str, int] | None = None,
) -> list[str]:
    """Per-source counts. Warn only on errors or a usually-productive source going to zero."""
    skipped_by_id = {item["source_id"]: item.get("message") or "skipped" for item in skipped}
    errors_by_id: dict[str, str] = {}
    for item in errors:
        errors_by_id.setdefault(item.get("source_id") or "", item.get("message") or "error")
    lines: list[str] = []
    warnings: list[str] = []
    for source in sources:
        source_id = source["id"]
        item = stats.get(source_id) or {"fetched": 0, "hits": 0, "signals": 0}
        if source_id in skipped_by_id:
            lines.append(f"source {source_id}: fetched 0, prefilter 0, signals 0, skipped ({skipped_by_id[source_id]})")
            continue
        lines.append(
            f"source {source_id}: fetched {item.get('fetched', 0)}, "
            f"prefilter {item.get('hits', 0)}, signals {item.get('signals', 0)}"
        )
        message = errors_by_id.get(source_id)
        if message:
            warnings.append(f"{source_id}: {message}")
        elif item.get("fetched", 0) == 0 and (prior_counts or {}).get(source_id, 0) > 0:
            warnings.append(f"{source_id}: returned 0 documents")
    if warnings:
        lines.append("warnings: " + "; ".join(warnings))
    return lines


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value[:10]).date()
    except ValueError:
        return None


def _clean(value: str | None) -> str:
    text = re.sub(r"<[^>]+>", " ", value or "")
    text = text.replace("\xa0", " ").replace("&nbsp;", " ")
    return re.sub(r"\s+", " ", text).strip().strip('"').strip()


def _headline(summary: str | None) -> str:
    text = _clean(summary)
    text = re.sub(r"^(possible alpr item:|stance shift:)\s*", "", text, flags=re.I)
    sentence = re.split(r"(?<=[.!?])\s+", text)[0].strip()
    if len(sentence) > 170:
        sentence = sentence[:167].rsplit(" ", 1)[0].rstrip(",;:") + "."
    if sentence and sentence[-1] not in ".!?":
        sentence += "."
    return sentence or "Something changed on license plate readers."


def _short_link(url: str) -> str:
    text = url.replace("https://", "").replace("http://", "").strip("/")
    if len(text) > 48:
        text = text[:45].rsplit("/", 1)[0] or text[:45]
    return text or "Source"


def _quote(quote: str | None) -> str:
    text = _clean(quote)
    if len(text) > 280:
        text = text[:277].rsplit(" ", 1)[0].rstrip(",;:") + "..."
    return text


def _stance_for(signal: dict[str, Any]) -> str:
    stored = (signal.get("alpr_stance") or "").strip()
    if stored in {"Positive for ALPR", "Negative for ALPR", "Mixed for ALPR", "Neutral / Mixed for ALPR"}:
        return MIXED if stored == "Neutral / Mixed for ALPR" else stored
    return derive_alpr_stance(signal.get("summary"), signal.get("quote"), signal.get("key_date_meaning"))


def signal_card(signal: dict[str, Any], url: str | None = None) -> dict[str, str]:
    """The one layout Josh reads: stance, one sentence, quote, link."""
    return {
        "stance": badge(_stance_for(signal)),
        "headline": _headline(signal.get("summary")),
        "quote": _quote(signal.get("quote")),
        "url": url or "",
    }


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
    source_health: list[str] | None = None,
) -> tuple[str, str, str] | None:
    """Return (subject, plain text, html) when a signal scores 40 or higher."""
    del agencies, run_errors, sources_checked, weekly, recent_signals, lookback_days, source_health
    chosen = [signal for signal in signals if (signal.get("score") or 0) >= 40]
    chosen.sort(key=lambda signal: (-(signal.get("score") or 0), signal.get("id") or 0))
    if not chosen:
        return None
    count = len(chosen)
    subject = f"ALPR Update ({today.strftime('%b')} {today.day}): {count} signal" + ("" if count == 1 else "s")
    plain_blocks = []
    html_blocks = []
    colors = {
        "Positive for ALPR": "#1b7f3a",
        "Negative for ALPR": "#b42318",
        "Mixed for ALPR": "#b54708",
    }
    for signal in chosen:
        url = (documents.get(signal.get("document_id") or -1, {}) or {}).get("url") or ""
        card = signal_card(signal, url)
        label, title, quoted = card["stance"], card["headline"], card["quote"]
        plain = [f"**{label}** — {title}"]
        if quoted:
            plain.append(f"> \"{quoted}\"")
        if url:
            plain.append(url)
        plain_blocks.append("\n".join(plain))
        ink = colors.get(label, colors["Mixed for ALPR"])
        quote_html = (
            f"<blockquote style=\"margin:12px 0 0;padding:0 0 0 12px;border-left:3px solid #ddd;"
            f"font-size:17px;line-height:1.45;color:#1c1c1c;\">“{html.escape(quoted)}”</blockquote>"
            if quoted
            else ""
        )
        link_html = (
            f"<p style=\"margin:12px 0 0;\"><a href=\"{html.escape(url, quote=True)}\" "
            f"style=\"color:#0b57d0;font-size:16px;\">{html.escape(_short_link(url))}</a></p>"
            if url
            else ""
        )
        html_blocks.append(
            "<tr><td style=\"padding:0 0 28px;\">"
            f"<p style=\"margin:0;font-size:18px;line-height:1.4;color:#111;\">"
            f"<strong style=\"color:{ink};\">{html.escape(label)}</strong> — {html.escape(title)}</p>"
            f"{quote_html}{link_html}</td></tr>"
        )
    plain_body = ("\n\n".join(plain_blocks)).strip() + "\n"
    html_body = (
        "<!DOCTYPE html><html><body style=\"margin:0;background:#f6f6f4;\">"
        "<table role=\"presentation\" width=\"100%\" cellspacing=\"0\" cellpadding=\"0\" style=\"background:#f6f6f4;\">"
        "<tr><td align=\"center\" style=\"padding:28px 16px;\">"
        "<table role=\"presentation\" width=\"100%\" style=\"max-width:560px;background:#ffffff;border-radius:12px;padding:8px 8px 0;\">"
        f"<tr><td style=\"padding:20px 20px 8px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;\">"
        f"<p style=\"margin:0 0 22px;font-size:15px;color:#666;\">{html.escape(subject)}</p>"
        "<table role=\"presentation\" width=\"100%\">"
        + "".join(html_blocks)
        + "</table></td></tr></table></td></tr></table></body></html>"
    )
    return subject, plain_body, html_body


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


def send_gmail(
    subject: str,
    body: str,
    *,
    user: str,
    password: str,
    recipient: str,
    html_body: str | None = None,
) -> None:
    if recipient.lower() != DIGEST_RECIPIENT:
        raise RuntimeError(f"refusing to email {recipient}; digest recipient is fixed")
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = user
    message["To"] = recipient
    message.set_content(body)
    if html_body:
        message.add_alternative(html_body, subtype="html")
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
    html_body: str | None = None,
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
    send_gmail(subject, body, user=smtp_user, password=smtp_password, recipient=target, html_body=html_body)
    return "sent"
