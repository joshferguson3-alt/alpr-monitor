"""Daily run. Ten stages, and a failing source never stops the rest."""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from monitor.bills import bill_key, status_kind
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
from monitor.constants import CLASSIFY_DOC_CAP, CLASSIFY_WORKERS, PASSAGE_CAP, RUN_BUDGET_SECONDS
from monitor.digest import deliver, render_digest, source_health_lines
from monitor.http import HttpClient, RobotsDenied
from monitor.notion_sync import NotionSync, client_from_env, is_object_not_found
from monitor.passages import extract_passages
from monitor.prefilter import Hit, passes
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


class UnusableFetch(RuntimeError):
    """The article page could not be read and no feed text was available."""


def _is_expected_skip(exc: BaseException) -> bool:
    return isinstance(exc, (RobotsDenied, MissingKeyError, UnusableFetch))


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
    named_org_id, named_org_flag = resolve_organization(result.get("organization"), registry)
    org_id, org_flag = named_org_id, named_org_flag
    if not org_id and entity in registry.organizations_by_id:
        org_id = entity
        org_flag = False
    needs = 1 if result.get("needs_review") else 0
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
        "bill_id": bill_key(
            meta.get("source_id"),
            meta.get("url"),
            meta.get("title"),
            result.get("bill_id"),
            (meta.get("published_at") or "")[:4],
        ) or result.get("bill_id"),
        "quote": result.get("quote"),
        "confidence": result.get("confidence") if result.get("confidence") is not None else 0,
        "needs_review": needs,
    }
    if meta.get("doc_type") == "bill":
        status = _bill_status_line(meta.get("bill_text") or meta.get("text") or "")
        if status:
            signal["key_date_meaning"] = status
    agency = registry.agencies_by_id.get(agency_id) if agency_id else None
    person = registry.people_by_id.get(person_id) if person_id else None
    subject = None
    stance_changed = False
    # A source feed is not the speaker. Stance shifts need a named person or a named organization.
    if signal["stance"] and person_id:
        subject = ("person", person_id)
    elif signal["stance"] and named_org_id and result.get("organization"):
        subject = ("organization", named_org_id)
    if subject:
        prior = store.prior_stance(subject[0], subject[1], signal["document_id"], meta.get("published_at"))
        stance_changed = prior is not None and prior != signal["stance"]
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
    import httpx
    from bs4 import BeautifulSoup

    from monitor.pdftext import extract_pdf_text

    try:
        response = http.get(url)
    except httpx.HTTPStatusError as exc:
        if exc.response is not None and exc.response.status_code in {401, 403}:
            raise UnusableFetch(f"article fetch {exc.response.status_code}, skipped") from exc
        raise
    content_type = response.headers.get("content-type", "")
    if "pdf" in content_type or response.content[:4] == b"%PDF" or url.lower().endswith(".pdf"):
        return extract_pdf_text(response.content)
    return BeautifulSoup(response.text, "html.parser").get_text("\n", strip=True)


def _bill_status_line(text: str) -> str:
    for line in (text or "").splitlines():
        if line.lower().startswith("bill status:"):
            return line.split(":", 1)[1].strip()[:180]
    return ""


def _richer_bill(new: dict, old) -> bool:
    if status_kind(f"{new.get('summary') or ''} {new.get('key_date_meaning') or ''}") != status_kind(
        f"{old['summary'] or ''} {old['key_date_meaning'] or ''}"
    ):
        return True
    new_info = len(new.get("summary") or "") + len(new.get("quote") or "")
    old_info = len(old["summary"] or "") + len(old["quote"] or "")
    return new_info > old_info + 40


def _remember_signal(store: Store, signal: dict) -> int | None:
    """Insert a signal unless this document or this bill already has one."""
    bill = signal.get("bill_id") or ""
    if bill.count(":") == 2:
        existing = store.signal_by_bill(bill)
        if existing is not None:
            if _richer_bill(signal, existing):
                store.update_signal(int(existing["id"]), signal)
                return int(existing["id"])
            return None
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
    publish: bool = True,
    classify_limit: int | None = None,
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
    prior_document_counts = store.document_counts_by_source()
    deadline = time.monotonic() + RUN_BUDGET_SECONDS
    classify_limit = CLASSIFY_DOC_CAP if classify_limit is None else classify_limit
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
        source["id"]: {"fetched": 0, "in_window": 0, "hits": 0, "signals": 0} for source in due
    }
    youtube_sources = []
    for source in due:
        if time.monotonic() >= deadline:
            errors.append(
                {
                    "source_id": source["id"],
                    "message": "run budget reached, collection stopped so publish can run",
                }
            )
            break
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
                payload = doc.as_dict() if hasattr(doc, "as_dict") else dict(doc)
                if stored is None:
                    existing = store.adopt_richer_text(payload.get("url") or "", payload.get("text") or "")
                    if existing is None:
                        existing = store.fill_text_if_empty(payload.get("url") or "", payload.get("text") or "")
                    if existing is not None and existing["classified_at"] is None:
                        restored = dict(existing)
                        restored["text"] = payload.get("text") or restored.get("text")
                        restored["entity"] = source.get("entity")
                        restored["force_prefilter"] = bool(payload.get("force_prefilter"))
                        new_docs.append(restored)
                    continue
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
            if source.get("entity") in registry.agencies_by_id:
                store.reopen_unmatched_documents(source["id"])
            for row in store.documents_without_signals(source["id"], since, until):
                if row["id"] in already:
                    continue
                text = (row["text"] or "").strip()
                if not text:
                    try:
                        text = _refetch_text(http, row["url"])
                    except Exception as exc:
                        _record_source_result(source["id"], exc, errors, skipped)
                        continue
                if not text.strip():
                    skipped.append({"source_id": source["id"], "message": "no usable text, skipped"})
                    continue
                restored = dict(row)
                restored["text"] = text
                restored["entity"] = source.get("entity")
                new_docs.append(restored)
                already.add(row["id"])

    passed_agendas: list[dict] = []
    hit_docs: list[dict] = []
    for doc in new_docs:
        include_context = (
            doc.get("entity") in registry.agencies_by_id
            and doc.get("doc_type") in {"agenda", "minutes", "packet"}
        )
        keyword_hits = passes(doc.get("text") or "", registry.keywords, include_context=include_context)
        if not keyword_hits and doc.get("force_prefilter") and (doc.get("text") or "").strip():
            keyword_hits = [Hit(0, min(len(doc["text"]), 80), "query hit", True)]
        if not keyword_hits:
            if doc.get("id"):
                store.mark_classified(doc["id"])
            continue
        store.mark_prefilter(doc["id"], True)
        doc["passed_prefilter"] = 1
        doc["_hits"] = keyword_hits
        if doc.get("source_id") in source_stats:
            source_stats[doc["source_id"]]["hits"] += 1
        if doc.get("doc_type") in {"agenda", "minutes", "packet"}:
            passed_agendas.append(doc)
        hit_docs.append(doc)

    first_ids = {source["id"] for source in due if source.get("classify_first")}
    for doc in hit_docs:
        if doc.get("source_id") in first_ids:
            doc["_classify_first"] = True
    pinned = [doc for doc in hit_docs if doc.get("_classify_first")]
    rest = [doc for doc in hit_docs if not doc.get("_classify_first")]
    rest.sort(key=lambda doc: doc.get("published_at") or "", reverse=True)
    to_classify = pinned + rest[:classify_limit]
    deferred = len(hit_docs) - len(to_classify)
    if deferred:
        print(f"deferred {deferred} prefilter hits to the next run")
        errors.append(
            {
                "source_id": "classifier",
                "message": f"classified {len(to_classify)} newest documents, {deferred} left for the next run",
            }
        )

    created_ids: list[int] = []
    passages_classified = 0
    if to_classify:
        classifier = classifier or Classifier()

    def classify_document(doc: dict) -> tuple[dict, list[tuple[dict, dict]]]:
        agency = registry.agencies_by_id.get(doc.get("entity") or "")
        metas = []
        for passage in extract_passages(doc.get("text") or "", doc["_hits"])[:PASSAGE_CAP]:
            metas.append(
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
                    "bill_text": doc.get("text") or "",
                }
            )
        classified: list[tuple[dict, dict]] = []
        for meta in metas:
            result = classifier.classify(
                passage=meta["text"],
                doc_type=meta.get("doc_type") or "",
                title=meta.get("title") or "",
                published_at=meta.get("published_at"),
                agency_name=meta.get("agency_name"),
            )
            classified.append((meta, result))
        return doc, classified

    done = 0
    index = 0
    while index < len(to_classify) and time.monotonic() < deadline:
        batch = to_classify[index : index + CLASSIFY_WORKERS]
        index += len(batch)
        with ThreadPoolExecutor(max_workers=CLASSIFY_WORKERS) as pool:
            futures = [pool.submit(classify_document, doc) for doc in batch]
            for future in futures:
                try:
                    doc, classified = future.result()
                except Exception as exc:
                    errors.append({"source_id": "classifier", "message": safe_message(exc)})
                    continue
                for meta, result in classified:
                    passages_classified += 1
                    if not result.get("is_relevant"):
                        continue
                    signal, subject, stance_changed = _build_signal(result, meta, registry, store, today)
                    signal_id = _remember_signal(store, signal)
                    if signal_id is None:
                        continue
                    if meta.get("source_id") in source_stats:
                        source_stats[meta["source_id"]]["signals"] += 1
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
                            if meta.get("source_id") in source_stats:
                                source_stats[meta["source_id"]]["signals"] += 1
                store.mark_classified(doc["id"])
                done += 1
                if done % 25 == 0 or done == len(to_classify):
                    print(f"classified {done}/{len(to_classify)} documents")
    if index < len(to_classify):
        print(f"run budget reached after {done} documents; remaining hits resume next run")

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
                        "source_id": stored.get("source_id"),
                    }
                    signal, subject, stance_changed = _build_signal(result, meta, registry, store, today)
                    signal_id = _remember_signal(store, signal)
                    if signal_id is None:
                        continue
                    if meta.get("source_id") in source_stats:
                        source_stats[meta["source_id"]]["signals"] += 1
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

    if publish and sync_notion:
        has_databases = bool(notion and notion.signals_db and notion.agencies_db)
        if not has_databases:
            errors.append(
                {
                    "source_id": "notion",
                    "message": "NOTION_SIGNALS_DB_ID / NOTION_AGENCIES_DB_ID are absent, Notion sync skipped",
                }
            )
        else:
            _sync_notion(notion, store, registry, _signals_for_notion(store, signals), today, errors)

    health = source_health_lines(source_stats, due, skipped, errors, prior_document_counts)
    if publish and send_email:
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
            source_health=health,
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
        passages_classified=passages_classified,
        signals_created=len(created_ids),
        errors=errors,
        source_health=health,
    )
    print(f"agencies: {len(registry.agencies)}")
    print(f"sources: {len(registry.sources)}")
    print(f"docs_fetched: {docs_fetched}")
    print(f"docs_new: {len(new_docs)}")
    print(f"passages_classified: {passages_classified}")
    print(f"signals_created: {len(created_ids)}")
    print(
        f"collected: documents fetched {docs_fetched}, new {len(new_docs)}, signals {len(created_ids)}"
    )
    prefilter_hits = sum(item["hits"] for item in source_stats.values())
    print(f"prefilter_hits: {prefilter_hits}")
    for line in health:
        print(line)
    print(f"skipped: {len(skipped)}")
    for item in skipped:
        print(f"skipped {item['source_id']}: {item['message']}")
    print(f"errors: {len(errors)}")
    for error in errors:
        print(f"error {error['source_id']}: {error['message']}")
    return 0


def _as_eastern(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=ZoneInfo("America/New_York"))
    return value.astimezone(ZoneInfo("America/New_York"))


def _parse_started_at(value: str | None) -> datetime | None:
    if not value or not str(value).strip():
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo("UTC"))
    return parsed


def scheduled_hour_blocks(now: datetime | None = None, latest_started_at: str | None = None) -> bool:
    """One scheduled collection on or after 6:00 AM Eastern, even if GitHub starts late.

    A manual run does not set ALPR_SCHEDULED. Before 6 AM Eastern, wait.
    After 6, skip only when a run already started today at or after 6 AM Eastern.
    """
    if os.environ.get("ALPR_SCHEDULED") != "1":
        return False
    current = _as_eastern(now or datetime.now(ZoneInfo("America/New_York")))
    if current.hour < 6:
        return True
    started = _parse_started_at(latest_started_at)
    if started is None:
        return False
    started_ny = started.astimezone(ZoneInfo("America/New_York"))
    return started_ny.date() == current.date() and started_ny.hour >= 6


def _signals_for_notion(store: Store, fresh: list[dict]) -> list[dict]:
    """This run's signals plus any older signal that never received a Notion page."""
    import json

    by_id = {}
    for signal in fresh:
        by_id[signal["id"]] = signal
    for row in store.signals_missing_notion_page():
        if row["id"] in by_id:
            continue
        signal = dict(row)
        if isinstance(signal.get("vendors"), str):
            signal["vendors"] = json.loads(signal["vendors"])
        by_id[signal["id"]] = signal
    return list(by_id.values())


def _sync_notion(notion, store, registry, signals, today, errors) -> None:
    notion.notes.clear()
    for signal in signals:
        try:
            document = store.document(signal["document_id"])
            doc = dict(document) if document else {}
            page_id = notion.upsert_signal(
                signal,
                doc,
                registry.agencies_by_id.get(signal.get("agency_id") or ""),
            )
            if page_id:
                store.set_notion_page(signal["id"], page_id)
        except Exception as exc:
            errors.append({"source_id": "notion", "message": safe_message(exc)})
    try:
        recent = [dict(row) for row in store.recent_signals(90)]
        for row in recent:
            if isinstance(row.get("vendors"), str):
                import json
                row["vendors"] = json.loads(row["vendors"])
        notion.upsert_agencies(registry.agencies, recent, today)
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
    for note in notion.notes:
        errors.append({"source_id": "notion", "message": note})


def publish_only(
    registry: Registry,
    store: Store,
    *,
    notion: NotionSync | None = None,
    today: date | None = None,
    lookback_days: int | None = None,
    mailer=None,
    send_email: bool = True,
    sync_notion: bool = True,
) -> int:
    """Publish signals already stored by a collect/classify run, including one that stopped early."""
    today = today or datetime.now(ZoneInfo("America/New_York")).date()
    started = store.latest_started_at()
    signals = [dict(row) for row in store.signals_created_since(started)] if started else []
    for signal in signals:
        if isinstance(signal.get("vendors"), str):
            import json
            signal["vendors"] = json.loads(signal["vendors"])
    errors: list[dict[str, str]] = []
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
            _sync_notion(notion, store, registry, _signals_for_notion(store, signals), today, errors)
    if send_email:
        documents = {}
        for signal in signals:
            document = store.document(signal["document_id"])
            if document:
                documents[signal["document_id"]] = dict(document)
        rendered = render_digest(
            signals,
            documents=documents,
            agencies=registry.agencies_by_id,
            run_errors=errors,
            sources_checked=len(registry.sources),
            today=today,
            weekly=today.weekday() == 0,
            recent_signals=[dict(row) for row in store.recent_signals(90)],
            lookback_days=lookback_days,
            source_health=store.latest_source_health(),
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
                else:
                    print(f"digest sent: {subject}")
            except Exception as exc:
                errors.append({"source_id": "digest", "message": safe_message(exc)})
        else:
            print("digest not sent: no signal scored 40 or higher")
    for error in errors:
        print(f"error {error['source_id']}: {error['message']}")
    print(f"signals_published: {len(signals)}")
    return 0


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
    parser.add_argument("--no-publish", action="store_true", help="Collect and classify only.")
    parser.add_argument("--publish-only", action="store_true", help="Notion sync and digest for the latest run.")
    args = parser.parse_args(argv)
    lookback_days = args.lookback_days
    if lookback_days is None:
        raw_lookback = os.environ.get("ALPR_LOOKBACK_DAYS", "").strip()
        if raw_lookback:
            lookback_days = int(raw_lookback)

    if not args.dry_run and not args.publish_only and os.environ.get("ALPR_SCHEDULED") == "1":
        gate = Store(args.db)
        latest = gate.latest_started_at()
        gate.close()
        if scheduled_hour_blocks(latest_started_at=latest):
            hour = datetime.now(ZoneInfo("America/New_York")).hour
            print(
                f"Scheduled run skipped: America/New_York hour is {hour}; "
                "before 6 AM or today's run already started."
            )
            return 0
        hour = datetime.now(ZoneInfo("America/New_York")).hour
        print(f"Scheduled run collecting. America/New_York hour is {hour}.")
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
        if args.publish_only:
            return publish_only(
                registry,
                store,
                notion=notion,
                today=today,
                lookback_days=lookback_days,
                send_email=not args.no_email,
                sync_notion=not args.no_notion,
            )
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
            publish=not args.no_publish,
        )
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
