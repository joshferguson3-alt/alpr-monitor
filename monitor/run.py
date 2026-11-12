"""Daily run. Ten stages, and a failing source never stops the rest."""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import traceback
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from monitor.classify import Classifier
from monitor.collectors.civicplus import CivicPlusCollector
from monitor.collectors.congress import CongressCollector
from monitor.collectors.courtlistener import CourtListenerCollector
from monitor.collectors.flock_transparency import FlockTransparencyCollector
from monitor.collectors.generic_html import GenericHtmlCollector
from monitor.collectors.iqm2 import Iqm2Collector
from monitor.collectors.legiscan import LegiScanCollector
from monitor.collectors.legistar import LegistarCollector
from monitor.collectors.openstates import OpenStatesCollector
from monitor.collectors.primegov import PrimeGovCollector
from monitor.collectors.rss import RssCollector
from monitor.collectors.base import MissingKeyError
from monitor.collectors.youtube import YouTubeCollector
from monitor.constants import PASSAGE_CAP
from monitor.digest import deliver, render_digest
from monitor.http import HttpClient, RobotsDenied
from monitor.notion_sync import NotionSync, client_from_env, is_object_not_found
from monitor.passages import extract_passages
from monitor.prefilter import passes
from monitor.registry import Registry, load_registry
from monitor.resolve import resolve_agency, resolve_organization, resolve_person
from monitor.score import score_signal
from monitor.store import Store, utcnow

COLLECTORS = {
    "civicplus": CivicPlusCollector(),
    "generic_html": GenericHtmlCollector(),
    "rss": RssCollector(),
    "primegov": PrimeGovCollector(),
    "iqm2": Iqm2Collector(),
    "legistar": LegistarCollector(),
    "legiscan": LegiScanCollector(),
    "openstates": OpenStatesCollector(),
    "congress": CongressCollector(),
    "courtlistener": CourtListenerCollector(),
    "flock_transparency": FlockTransparencyCollector(),
    "youtube": YouTubeCollector(),
}

_SECRET = re.compile(r"(key=)[^&\s]+|(Token )[^\s]+|(Bearer )[^\s]+", re.I)


def safe_message(exc: BaseException) -> str:
    text = str(exc).strip() or exc.__class__.__name__
    text = _SECRET.sub(lambda match: (match.group(1) or match.group(2) or match.group(3) or "") + "REDACTED", text)
    return text[:500]


def cadence_due(source: dict, today: date) -> bool:
    cadence = source.get("cadence") or "daily"
    if cadence == "daily":
        return True
    if cadence == "weekly":
        return today.weekday() == 0
    if cadence == "monthly":
        return today.day == 1
    if cadence == "quarterly":
        return today.day == 1 and today.month in (1, 4, 7, 10)
    return True


def window_for(
    store: Store,
    source_id: str,
    today: date,
    lookback_days: int | None = None,
) -> tuple[date, date]:
    # until extends 14 days ahead so an agenda posted for an upcoming meeting is included.
    # A backfill lookback replaces the 14/90 rule for every source.
    if lookback_days is not None:
        return today - timedelta(days=int(lookback_days)), today + timedelta(days=14)
    lookback = 90 if store.source_document_count(source_id) == 0 else 14
    return today - timedelta(days=lookback), today + timedelta(days=14)


def _published_in_window(published_at: str | None, since: date, until: date) -> bool:
    if not published_at:
        return True
    try:
        day = date.fromisoformat(str(published_at)[:10])
    except ValueError:
        return True
    return since <= day <= until


def _is_expected_skip(exc: BaseException) -> bool:
    return isinstance(exc, (RobotsDenied, MissingKeyError))


def _record_source_result(source_id: str, exc: BaseException, errors: list, skipped: list) -> None:
    message = safe_message(exc)
    if _is_expected_skip(exc):
        if "skipped" not in message:
            message = f"skipped: {message}"
        skipped.append({"source_id": source_id, "message": message})
        return
    errors.append({"source_id": source_id, "message": message})


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _store_document(store: Store, doc) -> dict | None:
    payload = doc.as_dict() if hasattr(doc, "as_dict") else dict(doc)
    payload["content_hash"] = _hash(payload.get("text") or "")
    payload["fetched_at"] = utcnow()
    document_id = store.insert_document(payload)
    if document_id is None:
        return None
    payload["id"] = document_id
    return payload


def _video_matches(video, agendas: list[dict]) -> bool:
    published = getattr(video, "published_at", None)
    entity = getattr(video, "entity", None)
    if not published or not entity:
        return False
    try:
        video_day = date.fromisoformat(str(published)[:10])
    except ValueError:
        return False
    for agenda in agendas:
        if agenda.get("entity") != entity or not agenda.get("published_at"):
            continue
        try:
            agenda_day = date.fromisoformat(str(agenda["published_at"])[:10])
        except ValueError:
            continue
        if abs((video_day - agenda_day).days) <= 3:
            return True
    return False


def _build_signal(result: dict, meta: dict, registry: Registry, store: Store, today: date) -> tuple[dict, tuple | None, bool]:
    if not result.get("speaker_name") and meta.get("doc_type") == "bill":
        for person in registry.people:
            if person["name"] and person["name"] in (meta.get("text") or ""):
                result["speaker_name"] = person["name"]
                break
    agency_id, agency_flag = resolve_agency(result.get("agency_name"), registry)
    entity = meta.get("entity")
    if not agency_id and entity in registry.agencies_by_id:
        agency_id = entity
        agency_flag = False
    person_id, person_flag = resolve_person(result.get("speaker_name"), registry)
    org_id, org_flag = resolve_organization(result.get("organization"), registry)
    if not org_id and entity in registry.organizations_by_id:
        org_id = entity
        org_flag = False
    needs = 1 if result.get("needs_review") or agency_flag or (person_flag and result.get("speaker_name")) or (org_flag and result.get("organization")) else 0
    counts = result.get("resident_counts") or {}
    if not isinstance(counts, dict):
        counts = {}
    signal = {
        "document_id": meta["document_id"],
        "signal_type": result.get("signal_type") or "incident",
        "summary": (result.get("summary") or "ALPR mention.")[:500],
        "agency_id": agency_id,
        "agency_name_raw": result.get("agency_name"),
        "person_id": person_id,
        "speaker_name_raw": result.get("speaker_name"),
        "speaker_role": result.get("speaker_role"),
        "organization_id": org_id,
        "vendors": result.get("vendors") or [],
        "stance": result.get("stance"),
        "stance_target": result.get("stance_target"),
        "resident_support": counts.get("support"),
        "resident_oppose": counts.get("oppose"),
        "key_date": (result.get("key_date") or "")[:10] or None,
        "key_date_meaning": result.get("key_date_meaning"),
        "dollar_amount": result.get("dollar_amount"),
        "camera_count": result.get("camera_count"),
        "bill_id": result.get("bill_id"),
        "quote": result.get("quote"),
        "confidence": result.get("confidence") if result.get("confidence") is not None else 0,
        "needs_review": needs,
    }
    if not signal["agency_id"] and not signal["person_id"] and not signal["organization_id"]:
        signal["needs_review"] = 1
    agency = registry.agencies_by_id.get(agency_id) if agency_id else None
    person = registry.people_by_id.get(person_id) if person_id else None
    subject = None
    stance_changed = False
    if signal["stance"] and person_id:
        subject = ("person", person_id)
    elif signal["stance"] and org_id:
        subject = ("organization", org_id)
    if subject:
        last = store.last_stance(*subject)
        stance_changed = last is not None and last != signal["stance"]
    state = (agency or {}).get("state")
    score, tier = score_signal(
        signal,
        agency=agency,
        person=person,
        stance_changed=stance_changed,
        today=today,
        state=state,
    )
    signal["score"] = score
    signal["tier"] = tier
    return signal, subject, stance_changed


def _refetch_text(http: HttpClient, url: str) -> str:
    from bs4 import BeautifulSoup

    from monitor.pdftext import extract_pdf_text

    response = http.get(url)
    content_type = response.headers.get("content-type", "")
    if "pdf" in content_type or response.content[:4] == b"%PDF" or url.lower().endswith(".pdf"):
        return extract_pdf_text(response.content)
    return BeautifulSoup(response.text, "html.parser").get_text("\n", strip=True)


def _remember_signal(store: Store, signal: dict) -> int | None:
    """Insert a signal unless this document already has the same type, quote, and summary."""
    if store.has_signal(
        signal["document_id"],
        signal["signal_type"],
        signal.get("quote"),
        signal["summary"],
    ):
        return None
    return store.insert_signal(signal)


def execute(
    registry: Registry,
    store: Store,
    *,
    http: HttpClient | None = None,
    classifier: Classifier | None = None,
    notion: NotionSync | None = None,
    today: date | None = None,
    dry_run: bool = False,
    ignore_cadence: bool = False,
    send_email: bool = True,
    sync_notion: bool = True,
    mailer=None,
    lookback_days: int | None = None,
) -> int:
    today = today or datetime.now(ZoneInfo("America/New_York")).date()
    run_id = store.start_run()
    started = utcnow()
    if dry_run:
        store.finish_run(
            run_id,
            docs_fetched=0,
            docs_new=0,
            passages_classified=0,
            signals_created=0,
            errors=[],
        )
        print(f"agencies: {len(registry.agencies)}")
        print(f"sources: {len(registry.sources)}")
        return 0

    http = http or HttpClient()
    errors: list[dict[str, str]] = []
    skipped: list[dict[str, str]] = []
    docs_fetched = 0
    new_docs: list[dict] = []
    due = [
        source
        for source in registry.sources
        if ignore_cadence or cadence_due(source, today)
    ]

    def handle_source(source: dict) -> list:
        collector = COLLECTORS[source["collector"]]
        since, until = window_for(store, source["id"], today, lookback_days)
        if source["collector"] == "flock_transparency":
            previous = store.latest_snapshot(source["id"])
            source["_previous_snapshot"] = previous["data"] if previous else None
        if source["collector"] == "youtube":
            return []
        found = collector.collect(source, since=since, until=until, http=http)
        if source.get("_new_snapshot") is not None:
            store.insert_snapshot(source["id"], source["_new_snapshot"])
        return found

    source_stats: dict[str, dict[str, int]] = {
        source["id"]: {"fetched": 0, "in_window": 0, "hits": 0} for source in due
    }
    youtube_sources = []
    for source in due:
        if source.get("enabled") is False:
            reason = source.get("skip_reason") or "disabled, skipped"
            skipped.append({"source_id": source["id"], "message": reason})
            continue
        if source["collector"] == "youtube":
            youtube_sources.append(source)
            continue
        try:
            found = handle_source(source)
            docs_fetched += len(found)
            since, until = window_for(store, source["id"], today, lookback_days)
            source_stats[source["id"]]["fetched"] = len(found)
            source_stats[source["id"]]["in_window"] = sum(
                1 for doc in found if _published_in_window(getattr(doc, "published_at", None), since, until)
            )
            for doc in found:
                stored = _store_document(store, doc)
                if stored:
                    stored["entity"] = source.get("entity")
                    new_docs.append(stored)
        except Exception as exc:
            _record_source_result(source["id"], exc, errors, skipped)

    if lookback_days is not None:
        already = {doc["id"] for doc in new_docs}
        for source in due:
            if source.get("enabled") is False or source["collector"] == "youtube":
                continue
            since, until = window_for(store, source["id"], today, lookback_days)
            for row in store.documents_without_signals(source["id"], since, until):
                if row["id"] in already:
                    continue
                try:
                    text = _refetch_text(http, row["url"])
                except Exception as exc:
                    _record_source_result(source["id"], exc, errors, skipped)
                    continue
                restored = dict(row)
                restored["text"] = text
                restored["entity"] = source.get("entity")
                new_docs.append(restored)
                already.add(row["id"])

    passages: list[dict] = []
    passed_agendas: list[dict] = []
    for doc in new_docs:
        hits = passes(doc.get("text") or "", registry.keywords)
        if not hits:
            continue
        store.mark_prefilter(doc["id"], True)
        doc["passed_prefilter"] = 1
        if doc.get("source_id") in source_stats:
            source_stats[doc["source_id"]]["hits"] += 1
        if doc.get("doc_type") in {"agenda", "minutes", "packet"}:
            passed_agendas.append(doc)
        agency = registry.agencies_by_id.get(doc.get("entity") or "")
        for passage in extract_passages(doc.get("text") or "", hits):
            passages.append(
                {
                    "document_id": doc["id"],
                    "source_id": doc["source_id"],
                    "entity": doc.get("entity"),
                    "doc_type": doc.get("doc_type"),
                    "title": doc.get("title"),
                    "published_at": doc.get("published_at"),
                    "agency_name": agency["name"] if agency else None,
                    "text": passage,
                    "url": doc.get("url"),
                }
            )

    passages.sort(key=lambda item: item.get("published_at") or "", reverse=True)
    if len(passages) > PASSAGE_CAP:
        overflow = len(passages) - PASSAGE_CAP
        passages = passages[:PASSAGE_CAP]
        errors.append(
            {
                "source_id": "classifier",
                "message": f"passage cap of {PASSAGE_CAP} reached, {overflow} newer-first overflow left unclassified",
            }
        )

    created_ids: list[int] = []
    if passages:
        classifier = classifier or Classifier()
    for meta in passages:
        try:
            result = classifier.classify(
                passage=meta["text"],
                doc_type=meta.get("doc_type") or "",
                title=meta.get("title") or "",
                published_at=meta.get("published_at"),
                agency_name=meta.get("agency_name"),
            )
        except Exception as exc:
            errors.append({"source_id": meta["source_id"], "message": safe_message(exc)})
            continue
        if not result.get("is_relevant"):
            continue
        signal, subject, stance_changed = _build_signal(result, meta, registry, store, today)
        signal_id = _remember_signal(store, signal)
        if signal_id is None:
            continue
        signal["id"] = signal_id
        signal["created_at"] = utcnow()
        created_ids.append(signal_id)
        if subject and signal.get("stance"):
            store.insert_stance(subject[0], subject[1], signal["stance"], signal_id)
        if stance_changed:
            shift = dict(signal)
            shift["signal_type"] = "stance_statement"
            shift["summary"] = "Stance shift: " + (signal.get("summary") or "")
            agency = registry.agencies_by_id.get(shift.get("agency_id") or "")
            person = registry.people_by_id.get(shift.get("person_id") or "")
            score, tier = score_signal(
                shift,
                agency=agency,
                person=person,
                stance_changed=False,
                today=today,
                state=(agency or {}).get("state"),
            )
            shift["score"] = score
            shift["tier"] = tier
            shift.pop("id", None)
            shift.pop("created_at", None)
            shift_id = _remember_signal(store, shift)
            if shift_id is not None:
                created_ids.append(shift_id)

    # Meeting video: captions only when a keyword-passing agenda matches.
    known_agendas = list(passed_agendas)
    source_entity = {source["id"]: source.get("entity") for source in registry.sources}
    for row in store.passed_agendas():
        known_agendas.append(
            {
                "entity": source_entity.get(row["source_id"]),
                "published_at": row["published_at"],
                "doc_type": row["doc_type"],
            }
        )
    youtube = COLLECTORS["youtube"]
    for source in youtube_sources:
        try:
            since, until = window_for(store, source["id"], today, lookback_days)
            videos = youtube.list_videos(source, since=since, until=until, http=http)
            docs_fetched += len(videos)
            source_stats[source["id"]]["fetched"] = len(videos)
            source_stats[source["id"]]["in_window"] = sum(
                1 for video in videos if _published_in_window(video.published_at, since, until)
            )
            for video in videos:
                video.entity = source.get("entity")
                if not _video_matches(video, known_agendas):
                    continue
                if store.seen(video.url, _hash(video.title or video.url)):
                    # A title-only row may already exist. Captions are a different hash,
                    # but the URL unique constraint still blocks a second insert.
                    continue
                video = youtube.with_captions(video)
                stored = _store_document(store, video)
                if not stored:
                    continue
                stored["entity"] = source.get("entity")
                hits = passes(stored.get("text") or "", registry.keywords)
                if not hits:
                    continue
                store.mark_prefilter(stored["id"], True)
                source_stats[source["id"]]["hits"] += 1
                agency = registry.agencies_by_id.get(stored.get("entity") or "")
                extra = []
                for passage in extract_passages(stored.get("text") or "", hits):
                    extra.append(passage)
                if not classifier:
                    classifier = Classifier()
                for passage in extra[:5]:
                    result = classifier.classify(
                        passage=passage,
                        doc_type="transcript",
                        title=stored.get("title") or "",
                        published_at=stored.get("published_at"),
                        agency_name=agency["name"] if agency else None,
                    )
                    if not result.get("is_relevant"):
                        continue
                    meta = {
                        "document_id": stored["id"],
                        "entity": stored.get("entity"),
                    }
                    signal, subject, stance_changed = _build_signal(result, meta, registry, store, today)
                    signal_id = _remember_signal(store, signal)
                    if signal_id is None:
                        continue
                    created_ids.append(signal_id)
                    if subject and signal.get("stance"):
                        store.insert_stance(subject[0], subject[1], signal["stance"], signal_id)
        except Exception as exc:
            _record_source_result(source["id"], exc, errors, skipped)

    signals = [dict(row) for row in store.signals_created_since(started)]
    for signal in signals:
        if isinstance(signal.get("vendors"), str):
            import json
            signal["vendors"] = json.loads(signal["vendors"])

    if sync_notion:
        has_databases = bool(notion and notion.signals_db and notion.agencies_db)
        if not has_databases:
            errors.append(
                {
                    "source_id": "notion",
                    "message": "NOTION_SIGNALS_DB_ID / NOTION_AGENCIES_DB_ID are absent, Notion sync skipped",
                }
            )
        else:
            _sync_notion(notion, store, registry, signals, today, errors)

    if send_email:
        documents = {}
        for signal in signals:
            document = store.document(signal["document_id"])
            if document:
                documents[signal["document_id"]] = dict(document)
        weekly = today.weekday() == 0
        recent = [dict(row) for row in store.recent_signals(90)] if weekly else signals
        rendered = render_digest(
            signals,
            documents=documents,
            agencies=registry.agencies_by_id,
            run_errors=errors,
            sources_checked=len(due),
            today=today,
            weekly=weekly,
            recent_signals=recent,
            lookback_days=lookback_days,
        )
        if rendered:
            subject, body = rendered
            try:
                status = deliver(
                    subject,
                    body,
                    smtp_user=os.environ.get("SMTP_USER"),
                    smtp_password=os.environ.get("SMTP_APP_PASSWORD"),
                    recipient=os.environ.get("DIGEST_TO"),
                    sender=mailer,
                )
                if status != "sent":
                    errors.append({"source_id": "digest", "message": status})
            except Exception as exc:
                errors.append({"source_id": "digest", "message": safe_message(exc)})

    store.finish_run(
        run_id,
        docs_fetched=docs_fetched,
        docs_new=len(new_docs),
        passages_classified=len(passages),
        signals_created=len(created_ids),
        errors=errors,
    )
    print(f"agencies: {len(registry.agencies)}")
    print(f"sources: {len(registry.sources)}")
    print(f"docs_fetched: {docs_fetched}")
    print(f"docs_new: {len(new_docs)}")
    print(f"passages_classified: {len(passages)}")
    print(f"signals_created: {len(created_ids)}")
    prefilter_hits = sum(item["hits"] for item in source_stats.values())
    print(f"prefilter_hits: {prefilter_hits}")
    for source_id, item in source_stats.items():
        print(
            f"source {source_id}: fetched {item['fetched']}, in_window {item['in_window']}, hits {item['hits']}"
        )
    print(f"skipped: {len(skipped)}")
    for item in skipped:
        print(f"skipped {item['source_id']}: {item['message']}")
    print(f"errors: {len(errors)}")
    for error in errors:
        print(f"error {error['source_id']}: {error['message']}")
    return 0


def scheduled_hour_blocks(now: datetime | None = None) -> bool:
    """Scheduled runs wait for 6:00 AM Eastern. workflow_dispatch does not set ALPR_SCHEDULED."""
    if os.environ.get("ALPR_SCHEDULED") != "1":
        return False
    current = now or datetime.now(ZoneInfo("America/New_York"))
    return current.hour != 6


def _sync_notion(notion, store, registry, signals, today, errors) -> None:
    try:
        for signal in signals:
            document = store.document(signal["document_id"])
            doc = dict(document) if document else {}
            page_id = notion.upsert_signal(
                signal,
                doc,
                registry.agencies_by_id.get(signal.get("agency_id") or ""),
            )
            if page_id:
                store.set_notion_page(signal["id"], page_id)
        recent = [dict(row) for row in store.recent_signals(90)]
        for row in recent:
            if isinstance(row.get("vendors"), str):
                import json
                row["vendors"] = json.loads(row["vendors"])
        notion.upsert_agencies(registry.agencies, recent, today)
        for note in notion.notes:
            errors.append({"source_id": "notion", "message": note})
    except Exception as exc:
        if is_object_not_found(exc):
            errors.append(
                {
                    "source_id": "notion",
                    "message": "Notion returned object_not_found. The integration may not have access yet.",
                }
            )
        else:
            errors.append({"source_id": "notion", "message": safe_message(exc)})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ALPR Signal Monitor")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--config", default="config")
    parser.add_argument("--db", default="data/monitor.sqlite")
    parser.add_argument("--all-cadences", action="store_true")
    parser.add_argument("--no-email", action="store_true")
    parser.add_argument("--no-notion", action="store_true")
    parser.add_argument("--now", default=None, help="YYYY-MM-DD override, used by tests")
    parser.add_argument("--lookback-days", type=int, default=None, help="Backfill window. Replaces the 14/90 rule.")
    args = parser.parse_args(argv)
    lookback_days = args.lookback_days
    if lookback_days is None:
        raw_lookback = os.environ.get("ALPR_LOOKBACK_DAYS", "").strip()
        if raw_lookback:
            lookback_days = int(raw_lookback)

    if not args.dry_run and scheduled_hour_blocks():
        hour = datetime.now(ZoneInfo("America/New_York")).hour
        print(f"Scheduled run skipped: America/New_York hour is {hour}, not 6.")
        return 0
    if not args.dry_run and os.environ.get("ALPR_SCHEDULED") != "1":
        print("Manual run: 6 AM Eastern gate skipped.")

    registry = load_registry(args.config)
    store = Store(args.db)
    today = date.fromisoformat(args.now) if args.now else None
    notion = None
    if not args.no_notion and not args.dry_run:
        client = client_from_env()
        if client is not None:
            notion = NotionSync(client)
    try:
        return execute(
            registry,
            store,
            today=today,
            dry_run=args.dry_run,
            ignore_cadence=args.all_cadences or os.environ.get("ALPR_SCHEDULED") != "1",
            send_email=not args.no_email and not args.dry_run,
            sync_notion=not args.no_notion and not args.dry_run,
            notion=notion,
            lookback_days=lookback_days,
        )
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
