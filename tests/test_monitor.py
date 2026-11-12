"""Milestone tests. Network calls are mocked. Live classification is separate."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import date
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
sys.path.insert(0, str(ROOT))

from monitor.collectors.base import MissingKeyError  # noqa: E402
from monitor.collectors.civicplus import CivicPlusCollector  # noqa: E402
from monitor.collectors.flock_transparency import describe_change, extract_fields  # noqa: E402
from monitor.collectors.iqm2 import Iqm2Collector  # noqa: E402
from monitor.collectors.legiscan import LegiScanCollector  # noqa: E402
from monitor.http import HttpClient, RobotsDenied  # noqa: E402
from monitor.pdftext import extract_pdf_text  # noqa: E402
from monitor.prefilter import passes  # noqa: E402
from monitor.registry import load_registry  # noqa: E402
from monitor.run import COLLECTORS, execute  # noqa: E402
from monitor.score import account_temperature, score_signal  # noqa: E402
from monitor.store import Store  # noqa: E402

TODAY = date(2026, 11, 12)
ALPR = (FIXTURES / "alpr_agenda.txt").read_text(encoding="utf-8")


def _allow_robots(url: str) -> bool:
    return url.endswith("/robots.txt")


class Handler:
    def __init__(self, routes: dict[str, httpx.Response | Exception]):
        self.routes = routes

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/robots.txt") and url not in self.routes:
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        for key, value in self.routes.items():
            if key in url:
                if isinstance(value, Exception):
                    raise value
                return value
        return httpx.Response(404, text=f"missing {url}")


def client_for(routes: dict) -> HttpClient:
    transport = httpx.MockTransport(Handler(routes))
    return HttpClient(transport=transport, sleep=lambda _seconds: None, min_delay=0)


def copy_config(tmp: Path, sources: list[dict] | None = None) -> Path:
    config = tmp / "config"
    config.mkdir()
    for name in ("agencies.yaml", "people.yaml", "organizations.yaml", "keywords.yaml", "sources.yaml"):
        (config / name).write_text((ROOT / "config" / name).read_text(encoding="utf-8"), encoding="utf-8")
    if sources is not None:
        import yaml

        (config / "sources.yaml").write_text(yaml.safe_dump(sources, sort_keys=False), encoding="utf-8")
    return config


class QuoteClassifier:
    """Returns one relevant contract signal whose quote is copied from the passage."""

    def __init__(self, stance: str | None = None, signal_type: str = "contract_event"):
        self.calls = 0
        self.stance = stance
        self.signal_type = signal_type
        self.passages: list[str] = []

    def classify(self, **kwargs):
        self.calls += 1
        passage = kwargs["passage"]
        self.passages.append(passage)
        quote = "Flock Safety license plate reader"
        if quote not in passage:
            quote = passage.strip().split("\n")[0][:180]
        result = {
            "is_relevant": True,
            "signal_type": self.signal_type,
            "summary": "Council discussed the Flock Safety license plate reader contract.",
            "agency_name": kwargs.get("agency_name"),
            "vendors": ["Flock Safety"],
            "speaker_name": None,
            "speaker_role": None,
            "organization": None,
            "stance": self.stance,
            "stance_target": "incumbent_vendor" if self.stance else None,
            "resident_counts": None,
            "key_date": "2026-12-08",
            "key_date_meaning": "contract end",
            "dollar_amount": 48000,
            "camera_count": 12,
            "bill_id": None,
            "quote": quote,
            "confidence": 1,
        }
        from monitor.classify import finalize

        return finalize(result, passage)


def test_dry_run_prints_counts(tmp_path):
    db = tmp_path / "monitor.sqlite"
    result = subprocess.run(
        [sys.executable, "-m", "monitor.run", "--dry-run", "--db", str(db), "--config", str(ROOT / "config")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    registry = load_registry(ROOT / "config")
    assert f"agencies: {len(registry.agencies)}" in result.stdout
    assert f"sources: {len(registry.sources)}" in result.stdout
    assert len(registry.agencies) == 6
    store = Store(db)
    row = store.conn.execute("SELECT COUNT(*) AS n FROM runs").fetchone()
    assert row["n"] == 1
    store.close()


def test_registry_segments_match_counts():
    registry = load_registry(ROOT / "config")
    counts = {agency["id"]: agency["sworn_officers"] for agency in registry.agencies}
    assert counts["brookhaven-pd"] == 100
    assert counts["dunwoody-pd"] == 66
    assert counts["sandy-springs-pd"] == 152
    assert counts["marietta-pd"] == 139
    assert counts["roswell-pd"] == 160
    assert counts["johns-creek-pd"] == 78
    for agency in registry.agencies:
        assert 41 <= agency["sworn_officers"] <= 550


def test_saved_pdf_fixture_yields_a_passage():
    text = extract_pdf_text((FIXTURES / "sample_agenda.pdf").read_bytes())
    keywords = load_registry(ROOT / "config").keywords
    hits = passes(text, keywords)
    assert hits
    from monitor.passages import extract_passages

    assert extract_passages(text, hits)


def test_prefilter_flock_and_lpr():
    keywords = load_registry(ROOT / "config").keywords
    assert not passes("The council discussed Flock today.", keywords)
    assert passes("The council discussed Flock and the camera contract today.", keywords)
    assert passes("flock camera contract", keywords)
    assert not passes("a flock of geese on the lake", keywords)
    assert not passes("Flock is a common word.", keywords)
    assert not passes("The report mentions LPR only.", keywords)
    assert passes("The LPR policy covers data retention.", keywords)
    assert passes("A license plate reader contract is on the agenda.", keywords)


def test_civicplus_fixture_dedupes_and_yields_a_passage(tmp_path):
    listing = (FIXTURES / "civicplus_listing.html").read_text(encoding="utf-8")
    http = client_for(
        {
            "AgendaCenter": httpx.Response(200, text=listing),
            "agenda.html": httpx.Response(200, text=ALPR, headers={"content-type": "text/html"}),
        }
    )
    config = copy_config(
        tmp_path,
        [
            {
                "id": "marietta-meetings",
                "collector": "civicplus",
                "entity": "marietta-pd",
                "url": "https://example.test/AgendaCenter",
                "mode": "agenda_center",
                "cadence": "daily",
                "doc_type": "agenda",
            }
        ],
    )
    registry = load_registry(config)
    store = Store(tmp_path / "monitor.sqlite")
    classifier = QuoteClassifier()
    execute(registry, store, http=http, classifier=classifier, today=TODAY, ignore_cadence=True, send_email=False, sync_notion=False)
    first = store.conn.execute("SELECT docs_new, passages_classified FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    assert first["docs_new"] >= 1
    assert first["passages_classified"] >= 1
    execute(registry, store, http=http, classifier=classifier, today=TODAY, ignore_cadence=True, send_email=False, sync_notion=False)
    second = store.conn.execute("SELECT docs_new FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    assert second["docs_new"] == 0
    store.close()
    http.close()


def test_real_marietta_rows_parse():
    html = (FIXTURES / "marietta_agenda_rows.html").read_text(encoding="utf-8")
    http = client_for(
        {
            "AgendaCenter": httpx.Response(200, text=html),
            "ViewFile": httpx.Response(200, text="City council agenda packet.", headers={"content-type": "text/html"}),
        }
    )
    docs = CivicPlusCollector().collect(
        {
            "id": "marietta-meetings",
            "url": "https://www.mariettaga.gov/AgendaCenter",
            "mode": "agenda_center",
            "max_docs": 4,
        },
        since=date(2026, 8, 1),
        until=date(2026, 11, 30),
        http=http,
    )
    assert docs
    assert any("2026-11-09" in (doc.title + (doc.published_at or "")) or doc.published_at == "2026-11-09" for doc in docs)
    http.close()


def test_scores_match_hand_calculation():
    today = date(2026, 11, 12)
    agency = {"id": "marietta-pd", "alpr_vendor": "flock", "state": "GA"}
    person = {"on_governing_body": True}

    score, tier = score_signal(
        {
            "signal_type": "contract_event",
            "confidence": 1,
            "key_date": "2026-12-08",
            "summary": "Discussion of termination of the camera contract.",
            "quote": "",
            "key_date_meaning": "",
        },
        agency=agency,
        person=None,
        stance_changed=False,
        today=today,
    )
    assert (score, tier) == (100, "Act now")  # 60+20+15+10 = 105, capped

    score, tier = score_signal(
        {"signal_type": "contract_event", "confidence": 0.5, "summary": "", "quote": "", "key_date_meaning": ""},
        agency={"alpr_vendor": "unknown", "state": "GA"},
        person=None,
        stance_changed=False,
        today=today,
    )
    assert (score, tier) == (30, "Background")  # 60 * 0.5

    score, tier = score_signal(
        {
            "signal_type": "legislation_event",
            "confidence": 0.8,
            "summary": "The bill passed the House and a hearing is scheduled.",
            "quote": "",
            "key_date_meaning": "",
        },
        agency=None,
        person=None,
        stance_changed=False,
        today=today,
    )
    assert (score, tier) == (76, "Act now")  # (40+20+35) * 0.8 = 76

    score, tier = score_signal(
        {
            "signal_type": "public_sentiment",
            "confidence": 1,
            "resident_support": 1,
            "resident_oppose": 6,
            "summary": "",
            "quote": "",
            "key_date_meaning": "",
        },
        agency=agency,
        person=None,
        stance_changed=False,
        today=today,
    )
    assert (score, tier) == (50, "Watch")  # 25+15+10

    score, tier = score_signal(
        {"signal_type": "stance_statement", "confidence": 1, "summary": "", "quote": "", "key_date_meaning": ""},
        agency=agency,
        person=person,
        stance_changed=True,
        today=today,
    )
    assert (score, tier) == (65, "Watch")  # 30+20+15

    score, tier = score_signal(
        {"signal_type": "vendor_move", "confidence": 1, "summary": "", "quote": "", "key_date_meaning": ""},
        agency=None,
        person=None,
        stance_changed=False,
        today=today,
        state="GA",
    )
    assert (score, tier) == (55, "Watch")  # 35+20

    score, tier = score_signal(
        {
            "signal_type": "legislation_event",
            "confidence": 1,
            "summary": "The governor signed the bill into law.",
            "quote": "",
            "key_date_meaning": "",
        },
        agency=None,
        person=None,
        stance_changed=False,
        today=today,
    )
    assert score == 90  # 40+50

    score, tier = score_signal(
        {
            "signal_type": "public_sentiment",
            "confidence": 0.5,
            "resident_support": 0,
            "resident_oppose": 1,
            "summary": "",
            "quote": "",
            "key_date_meaning": "",
        },
        agency=None,
        person=None,
        stance_changed=False,
        today=today,
    )
    assert score == 13  # 25 * 0.5 = 12.5 -> 13
    assert score < 20


def test_privacy_drops_resident_names():
    from monitor.classify import finalize

    passage = "A resident said the cameras should be removed."
    result = finalize(
        {
            "is_relevant": True,
            "signal_type": "public_sentiment",
            "summary": "Jane Resident opposed the cameras.",
            "speaker_name": "Jane Resident",
            "speaker_role": "resident",
            "quote": "A resident said the cameras should be removed.",
            "confidence": 0.9,
        },
        passage,
    )
    assert result["speaker_name"] is None
    assert "Jane Resident" not in (result.get("summary") or "")


def test_primegov_robots_skip_does_not_stop_the_run(tmp_path, capsys):
    http = client_for(
        {
            "https://blocked.test/robots.txt": httpx.Response(200, text=(FIXTURES / "primegov_robots.txt").read_text()),
            "https://example.test/news": httpx.Response(
                200,
                text='<a href="https://example.test/item">license plate reader contract</a>',
            ),
        }
    )
    # The handler matches by substring. robots.txt for blocked.test must win.
    config = copy_config(
        tmp_path,
        [
            {
                "id": "brookhaven-meetings",
                "collector": "primegov",
                "entity": "brookhaven-pd",
                "url": "https://blocked.test/portal",
                "cadence": "daily",
            },
            {
                "id": "marietta-news",
                "collector": "generic_html",
                "entity": "marietta-pd",
                "url": "https://example.test/news",
                "cadence": "daily",
                "selectors": {"item": "a", "title": "a", "link": "a"},
            },
        ],
    )
    registry = load_registry(config)
    store = Store(tmp_path / "m.sqlite")
    execute(
        registry,
        store,
        http=http,
        classifier=QuoteClassifier(),
        today=TODAY,
        ignore_cadence=True,
        send_email=False,
        sync_notion=False,
    )
    errors = json.loads(store.conn.execute("SELECT errors FROM runs ORDER BY id DESC LIMIT 1").fetchone()["errors"])
    assert not any("brookhaven-meetings" == err["source_id"] for err in errors)
    captured = capsys.readouterr().out
    assert "skipped brookhaven-meetings:" in captured
    assert "robots" in captured
    assert store.conn.execute("SELECT COUNT(*) AS n FROM documents").fetchone()["n"] >= 1
    store.close()
    http.close()


def test_direct_robots_denial():
    http = client_for({"https://blocked.test/robots.txt": httpx.Response(200, text="User-agent: *\nDisallow: /\n")})
    with pytest.raises(RobotsDenied):
        http.get("https://blocked.test/portal")
    http.close()


def test_six_agencies_and_a_broken_url(tmp_path):
    agencies = [
        "brookhaven-pd",
        "dunwoody-pd",
        "sandy-springs-pd",
        "marietta-pd",
        "roswell-pd",
        "johns-creek-pd",
    ]
    sources = []
    routes = {}
    for agency in agencies:
        url = f"https://example.test/{agency}"
        sources.append(
            {
                "id": agency + "-page",
                "collector": "generic_html",
                "entity": agency,
                "url": url,
                "cadence": "daily",
                "doc_type": "press",
                "selectors": {"item": "a", "title": "a", "link": "a"},
            }
        )
        routes[url] = httpx.Response(
            200,
            text=f'<a href="{url}/item">A license plate reader update for {agency}</a>',
        )
    sources.append(
        {
            "id": "broken-source",
            "collector": "generic_html",
            "entity": "marietta-pd",
            "url": "https://example.test/broken",
            "cadence": "daily",
            "selectors": {"item": "a", "title": "a", "link": "a"},
        }
    )
    routes["https://example.test/broken"] = httpx.Response(500, text="down")
    http = client_for(routes)
    registry = load_registry(copy_config(tmp_path, sources))
    store = Store(tmp_path / "m.sqlite")
    code = execute(
        registry,
        store,
        http=http,
        classifier=QuoteClassifier(),
        today=TODAY,
        ignore_cadence=True,
        send_email=False,
        sync_notion=False,
    )
    assert code == 0
    rows = store.conn.execute("SELECT source_id FROM documents").fetchall()
    found = {row["source_id"] for row in rows}
    assert found == {agency + "-page" for agency in agencies}
    errors = json.loads(store.conn.execute("SELECT errors FROM runs").fetchone()["errors"])
    assert any(err["source_id"] == "broken-source" for err in errors)
    store.close()
    http.close()


def test_missing_legiscan_key_is_skipped(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("LEGISCAN_API_KEY", raising=False)
    http = client_for({})
    registry = load_registry(
        copy_config(
            tmp_path,
            [{"id": "ga-bills", "collector": "legiscan", "state": "GA", "cadence": "daily"}],
        )
    )
    store = Store(tmp_path / "m.sqlite")
    execute(registry, store, http=http, today=TODAY, ignore_cadence=True, send_email=False, sync_notion=False)
    errors = json.loads(store.conn.execute("SELECT errors FROM runs").fetchone()["errors"])
    output = capsys.readouterr().out
    assert not any(err["source_id"] == "ga-bills" for err in errors)
    assert "skipped ga-bills:" in output
    assert "missing key, skipped" in output
    store.close()


def test_legiscan_fixture_resolves_sponsor(tmp_path, monkeypatch):
    monkeypatch.setenv("LEGISCAN_API_KEY", "test-key")
    search = json.loads((FIXTURES / "legiscan_search.json").read_text())
    bill = json.loads((FIXTURES / "legiscan_bill.json").read_text())
    tn_search = json.loads(json.dumps(search))
    tn_search["searchresult"]["0"]["state"] = "TN"
    tn_search["searchresult"]["0"]["bill_id"] = 200
    tn_search["searchresult"]["0"]["bill_number"] = "SB200"
    tn_search["searchresult"]["0"]["url"] = "https://legiscan.com/TN/bill/SB200/2026"
    tn_bill = json.loads(json.dumps(bill))
    tn_bill["bill"]["bill_id"] = 200
    tn_bill["bill"]["sponsors"] = [{"name": "James W. Conroy"}]
    tn_bill["bill"]["url"] = "https://legiscan.com/TN/bill/SB200/2026"

    def response_for(url: str):
        if "state=TN" in url and "getSearch" in url:
            return httpx.Response(200, json=tn_search)
        if "getBill" in url and "id=200" in url:
            return httpx.Response(200, json=tn_bill)
        if "state=GA" in url and "getSearch" in url:
            return httpx.Response(200, json=search)
        if "getBill" in url and "id=100" in url:
            return httpx.Response(200, json=bill)
        return httpx.Response(404, text=url)

    transport = httpx.MockTransport(lambda request: response_for(str(request.url)) if not str(request.url).endswith("robots.txt") else httpx.Response(200, text="User-agent: *\nAllow: /\n"))
    http = HttpClient(transport=transport, sleep=lambda _s: None, min_delay=0)
    registry = load_registry(
        copy_config(
            tmp_path,
            [
                {"id": "ga-bills", "collector": "legiscan", "state": "GA", "cadence": "daily"},
                {"id": "tn-bills", "collector": "legiscan", "state": "TN", "cadence": "daily"},
            ],
        )
    )
    store = Store(tmp_path / "m.sqlite")
    execute(
        registry,
        store,
        http=http,
        classifier=QuoteClassifier(signal_type="legislation_event"),
        today=TODAY,
        ignore_cadence=True,
        send_email=False,
        sync_notion=False,
    )
    people = {row["person_id"] for row in store.conn.execute("SELECT person_id FROM signals")}
    assert "brandon-gurley" in people
    assert "james-conroy" in people
    states = {row["source_id"] for row in store.conn.execute("SELECT source_id FROM documents")}
    assert states == {"ga-bills", "tn-bills"}
    store.close()


def test_rss_fixture_uses_the_shared_pipeline(tmp_path):
    feed = (FIXTURES / "sample_feed.xml").read_bytes()
    http = client_for({"feed.xml": httpx.Response(200, content=feed)})
    registry = load_registry(
        copy_config(
            tmp_path,
            [
                {
                    "id": "sample-news",
                    "collector": "rss",
                    "entity": "georgia-recorder",
                    "url": "https://example.test/feed.xml",
                    "cadence": "daily",
                    "doc_type": "news",
                }
            ],
        )
    )
    store = Store(tmp_path / "m.sqlite")
    execute(
        registry,
        store,
        http=http,
        classifier=QuoteClassifier(),
        today=TODAY,
        ignore_cadence=True,
        send_email=False,
        sync_notion=False,
    )
    row = store.conn.execute("SELECT doc_type FROM documents").fetchone()
    assert row["doc_type"] == "news"
    assert store.conn.execute("SELECT COUNT(*) AS n FROM signals").fetchone()["n"] >= 1
    store.close()


def test_portal_change_produces_one_signal(tmp_path):
    before = (FIXTURES / "flock_before.html").read_text(encoding="utf-8")
    after = (FIXTURES / "flock_after.html").read_text(encoding="utf-8")
    assert describe_change(extract_fields(before), extract_fields(after))
    pages = {"body": before}

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("robots.txt"):
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        return httpx.Response(200, text=pages["body"])

    http = HttpClient(transport=httpx.MockTransport(handler), sleep=lambda _s: None, min_delay=0)
    registry = load_registry(
        copy_config(
            tmp_path,
            [
                {
                    "id": "flock-portal",
                    "collector": "flock_transparency",
                    "entity": "flock-safety",
                    "url": "https://example.test/transparency",
                    "cadence": "weekly",
                }
            ],
        )
    )
    store = Store(tmp_path / "m.sqlite")
    classifier = QuoteClassifier(signal_type="vendor_move")
    execute(registry, store, http=http, classifier=classifier, today=TODAY, ignore_cadence=True, send_email=False, sync_notion=False)
    assert store.conn.execute("SELECT COUNT(*) AS n FROM signals").fetchone()["n"] == 0
    assert store.conn.execute("SELECT COUNT(*) AS n FROM snapshots").fetchone()["n"] == 1
    pages["body"] = after
    execute(registry, store, http=http, classifier=classifier, today=TODAY, ignore_cadence=True, send_email=False, sync_notion=False)
    assert store.conn.execute("SELECT COUNT(*) AS n FROM signals").fetchone()["n"] == 1
    store.close()


def test_youtube_captions_only_after_a_matching_agenda(tmp_path):
    feed = (FIXTURES / "youtube_feed.xml").read_bytes()
    calls = []

    def captions(video_id: str) -> str:
        calls.append(video_id)
        return "The council discussed the Flock Safety license plate reader contract."

    COLLECTORS["youtube"].caption_fetcher = captions
    http = client_for(
        {
            "agenda": httpx.Response(
                200,
                text='<a href="https://example.test/agenda-doc">license plate reader contract</a>',
            ),
            "videos.xml": httpx.Response(200, content=feed),
        }
    )
    registry = load_registry(
        copy_config(
            tmp_path,
            [
                {
                    "id": "marietta-agenda",
                    "collector": "generic_html",
                    "entity": "marietta-pd",
                    "url": "https://example.test/agenda",
                    "cadence": "daily",
                    "doc_type": "agenda",
                    "selectors": {"item": "a", "title": "a", "link": "a"},
                },
                {
                    "id": "marietta-video",
                    "collector": "youtube",
                    "entity": "marietta-pd",
                    "url": "https://www.youtube.com/channel/UCxxxxxxxxxxxxxxxxxxxxxx",
                    "channel_id": "UCxxxxxxxxxxxxxxxxxxxxxx",
                    "cadence": "daily",
                },
            ],
        )
    )
    # The generic agenda has no published_at, so force a date after collection by
    # using a document the youtube matcher can see. The feed video is 2026-11-09.
    store = Store(tmp_path / "m.sqlite")
    # Seed a passed agenda dated with the video before the run's youtube phase
    # by letting the pipeline classify the agenda, then patch its published_at.
    original = COLLECTORS["generic_html"].collect

    def collect_with_date(source, *, since, until, http):
        docs = original(source, since=since, until=until, http=http)
        for doc in docs:
            doc.published_at = "2026-11-09"
            doc.doc_type = "agenda"
        return docs

    COLLECTORS["generic_html"].collect = collect_with_date
    try:
        execute(
            registry,
            store,
            http=http,
            classifier=QuoteClassifier(),
            today=TODAY,
            ignore_cadence=True,
            send_email=False,
            sync_notion=False,
        )
    finally:
        COLLECTORS["generic_html"].collect = original
    assert calls == ["abcdefghijk"]
    store.close()


def test_stance_shift_signal(tmp_path):
    class Alternating:
        def __init__(self):
            self.n = 0

        def classify(self, **kwargs):
            self.n += 1
            stance = "supports" if self.n == 1 else "opposes"
            passage = kwargs["passage"]
            from monitor.classify import finalize

            return finalize(
                {
                    "is_relevant": True,
                    "signal_type": "stance_statement",
                    "summary": f"The chief {stance} the cameras.",
                    "speaker_name": "Brandon Gurley",
                    "speaker_role": "Chief of Police",
                    "stance": stance,
                    "stance_target": "incumbent_vendor",
                    "quote": "Flock Safety license plate reader" if "Flock Safety license plate reader" in passage else passage[:120],
                    "confidence": 1,
                    "vendors": ["Flock Safety"],
                    "agency_name": "Brookhaven Police Department",
                },
                passage,
            )

    http = client_for(
        {
            "/first": httpx.Response(200, text='<a href="https://example.test/first-item">Flock Safety license plate reader one</a>'),
            "/second": httpx.Response(200, text='<a href="https://example.test/second-item">Flock Safety license plate reader two</a>'),
        }
    )
    registry = load_registry(
        copy_config(
            tmp_path,
            [
                {
                    "id": "first",
                    "collector": "generic_html",
                    "entity": "brookhaven-pd",
                    "url": "https://example.test/first",
                    "cadence": "daily",
                    "selectors": {"item": "a", "title": "a", "link": "a"},
                },
                {
                    "id": "second",
                    "collector": "generic_html",
                    "entity": "brookhaven-pd",
                    "url": "https://example.test/second",
                    "cadence": "daily",
                    "selectors": {"item": "a", "title": "a", "link": "a"},
                },
            ],
        )
    )
    store = Store(tmp_path / "m.sqlite")
    execute(registry, store, http=http, classifier=Alternating(), today=TODAY, ignore_cadence=True, send_email=False, sync_notion=False)
    summaries = [row["summary"] for row in store.conn.execute("SELECT summary FROM signals")]
    assert any(summary.startswith("Stance shift:") for summary in summaries)
    store.close()


def test_temperature_weights():
    today = date(2026, 11, 12)
    signals = [
        {"signal_type": "contract_event", "score": 60, "created_at": "2026-11-01", "stance": None, "stance_target": None},
        {"signal_type": "policy_event", "score": 50, "created_at": "2026-11-02", "stance": "supports", "stance_target": "alpr_generally"},
        {"signal_type": "stance_statement", "score": 40, "created_at": "2026-01-01", "stance": "opposes", "stance_target": "incumbent_vendor"},
    ]
    # January signal is outside 90 days. Contract is full weight, policy is half.
    assert account_temperature(signals, today=today) == 85.0


def test_digest_and_notion_upsert_once(tmp_path, monkeypatch):
    monkeypatch.setenv("DIGEST_TO", "josh.ferguson3@gmail.com")
    sent = []

    def mailer(subject, body, recipient):
        sent.append((subject, body, recipient))

    class Pages:
        def __init__(self, owner):
            self.owner = owner

        def create(self, parent, properties):
            page_id = f"page-{len(self.owner.pages)+1}"
            self.owner.pages[page_id] = properties
            self.owner.creates.append(properties)
            return {"id": page_id}

        def update(self, page_id, properties):
            self.owner.updates.append(page_id)
            return {"id": page_id}

    class DataSources:
        def __init__(self, owner):
            self.owner = owner

        def query(self, data_source_id, filter, page_size=1):
            number = (filter.get("number") or {}).get("equals")
            if number is None:
                return {"results": []}
            for page_id, props in self.owner.pages.items():
                if (props.get("Signal ID") or {}).get("number") == number:
                    return {"results": [{"id": page_id}]}
            return {"results": []}

    class Databases:
        def retrieve(self, database_id):
            return {"id": database_id, "data_sources": [{"id": database_id + "-ds"}]}

    class FakeClient:
        def __init__(self):
            self.pages_store = {}
            self.creates = []
            self.updates = []
            self.pages = Pages(self)
            self.data_sources = DataSources(self)
            self.databases = Databases()

        @property
        def pages(self):
            return self._pages

        @pages.setter
        def pages(self, value):
            self._pages = value

    fake = FakeClient()
    # The property dance above is messy because Pages assigns self.pages.
    # Rebuild simply.
    class Simple:
        def __init__(self):
            self.store = {}
            self.creates = []
            self.updates = []

            class P:
                def create(inner, parent, properties):
                    page_id = f"page-{len(self.store)+1}"
                    self.store[page_id] = properties
                    self.creates.append(page_id)
                    return {"id": page_id}

                def update(inner, page_id, properties):
                    self.updates.append(page_id)
                    self.store[page_id] = properties
                    return {"id": page_id}

            class D:
                def query(inner, data_source_id, filter, page_size=1):
                    number = (filter.get("number") or {}).get("equals")
                    text = (filter.get("rich_text") or {}).get("equals")
                    for page_id, props in self.store.items():
                        if number is not None and (props.get("Signal ID") or {}).get("number") == number:
                            return {"results": [{"id": page_id}]}
                        rich = ((props.get("Agency ID") or {}).get("rich_text") or [{}])[0]
                        if text and rich.get("text", {}).get("content") == text:
                            return {"results": [{"id": page_id}]}
                    return {"results": []}

            class B:
                def retrieve(inner, database_id):
                    return {"id": database_id, "data_sources": [{"id": database_id}]}

            self.pages = P()
            self.data_sources = D()
            self.databases = B()

    from monitor.notion_sync import NotionSync

    notion = NotionSync(Simple(), signals_db="signals-db", agencies_db="agencies-db")
    http = client_for(
        {
            "news": httpx.Response(
                200,
                text='<a href="https://example.test/story">Flock Safety license plate reader contract</a>',
            )
        }
    )
    registry = load_registry(
        copy_config(
            tmp_path,
            [
                {
                    "id": "marietta-news",
                    "collector": "generic_html",
                    "entity": "marietta-pd",
                    "url": "https://example.test/news",
                    "cadence": "daily",
                    "selectors": {"item": "a", "title": "a", "link": "a"},
                }
            ],
        )
    )
    store = Store(tmp_path / "m.sqlite")
    execute(
        registry,
        store,
        http=http,
        classifier=QuoteClassifier(),
        notion=notion,
        today=TODAY,
        ignore_cadence=True,
        send_email=True,
        sync_notion=True,
        mailer=mailer,
    )
    assert len(sent) == 1
    assert sent[0][2] == "josh.ferguson3@gmail.com"
    assert sent[0][0].startswith("ALPR signals:")
    created_pages = len(notion.client.creates)
    assert created_pages >= 1
    execute(
        registry,
        store,
        http=http,
        classifier=QuoteClassifier(),
        notion=notion,
        today=TODAY,
        ignore_cadence=True,
        send_email=True,
        sync_notion=True,
        mailer=mailer,
    )
    assert len(sent) == 1
    assert len(notion.client.creates) == created_pages
    store.close()


def test_digest_refuses_other_recipients():
    from monitor.digest import deliver

    called = []
    status = deliver(
        "subject",
        "body",
        smtp_user="josh.ferguson3@gmail.com",
        smtp_password="secret",
        recipient="someoneelse@example.com",
        sender=lambda *args: called.append(args),
    )
    assert "not sent" in status
    assert called == []


def test_iqm2_fixture_finds_an_agenda():
    html = (FIXTURES / "roswell_iqm2_rows.html").read_text(encoding="utf-8")
    http = client_for(
        {
            "Default.aspx": httpx.Response(200, text=html),
            "FileOpen.aspx": httpx.Response(200, content=b"Agenda text about a license plate reader.", headers={"content-type": "text/plain"}),
        }
    )
    docs = Iqm2Collector().collect(
        {"id": "roswell-meetings", "url": "https://roswellcityga.iqm2.com/Citizens/Default.aspx", "max_docs": 2},
        since=date(2026, 11, 1),
        until=date(2026, 11, 30),
        http=http,
    )
    assert docs
    http.close()


def test_seed_script_does_not_write_config(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENSTATES_API_KEY", raising=False)
    before = (ROOT / "config" / "people.yaml").read_text(encoding="utf-8")
    result = subprocess.run([sys.executable, str(ROOT / "scripts" / "seed_registry.py")], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0
    assert "missing key, skipped" in result.stdout
    assert (ROOT / "config" / "people.yaml").read_text(encoding="utf-8") == before


def test_setup_reuses_existing_databases_and_can_create():
    from scripts.setup_notion import databases_under_parent, ensure_database

    class Children:
        def list(self, block_id, start_cursor=None, page_size=100):
            assert block_id == "parent-page"
            if start_cursor is None:
                return {
                    "results": [
                        {
                            "id": "signals-id",
                            "type": "child_database",
                            "child_database": {"title": "Signals"},
                        }
                    ],
                    "has_more": True,
                    "next_cursor": "page-2",
                }
            return {
                "results": [
                    {
                        "id": "agencies-id",
                        "type": "child_database",
                        "child_database": {"title": "Agencies"},
                    },
                    {"id": "note", "type": "paragraph"},
                ],
                "has_more": False,
            }

    class Blocks:
        children = Children()

    class Databases:
        def __init__(self):
            self.created = []

        def create(self, **kwargs):
            self.created.append(kwargs["title"][0]["text"]["content"])
            return {"id": "new-" + kwargs["title"][0]["text"]["content"], "data_sources": []}

    client = type("Client", (), {})()
    client.blocks = Blocks()
    client.databases = Databases()
    found = databases_under_parent(client, "parent-page")
    assert found == {"Signals": "signals-id", "Agencies": "agencies-id"}
    reused_id, created = ensure_database(client, "parent-page", "Signals", found, {})
    assert reused_id == "signals-id"
    assert created is None
    assert client.databases.created == []
    new_id, created = ensure_database(client, "parent-page", "Signals", {}, {"Name": {"title": {}}})
    assert new_id == "new-Signals"
    assert created["id"] == "new-Signals"
    assert client.databases.created == ["Signals"]


def test_manual_run_skips_the_six_am_gate(monkeypatch):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from monitor.run import scheduled_hour_blocks

    noon = datetime(2026, 11, 12, 12, tzinfo=ZoneInfo("America/New_York"))
    six = datetime(2026, 11, 12, 6, tzinfo=ZoneInfo("America/New_York"))
    monkeypatch.setenv("ALPR_SCHEDULED", "0")
    assert scheduled_hour_blocks(noon) is False
    monkeypatch.setenv("ALPR_SCHEDULED", "1")
    assert scheduled_hour_blocks(noon) is True
    assert scheduled_hour_blocks(six) is False


def test_missing_notion_database_ids_still_send_the_digest(tmp_path, monkeypatch):
    monkeypatch.setenv("DIGEST_TO", "josh.ferguson3@gmail.com")
    monkeypatch.delenv("NOTION_SIGNALS_DB_ID", raising=False)
    monkeypatch.delenv("NOTION_AGENCIES_DB_ID", raising=False)
    sent = []
    from monitor.notion_sync import NotionSync

    http = client_for(
        {
            "news": httpx.Response(
                200,
                text='<a href="https://example.test/story">Flock Safety license plate reader contract</a>',
            )
        }
    )
    registry = load_registry(
        copy_config(
            tmp_path,
            [
                {
                    "id": "marietta-news",
                    "collector": "generic_html",
                    "entity": "marietta-pd",
                    "url": "https://example.test/news",
                    "cadence": "daily",
                    "selectors": {"item": "a", "title": "a", "link": "a"},
                }
            ],
        )
    )
    store = Store(tmp_path / "m.sqlite")
    execute(
        registry,
        store,
        http=http,
        classifier=QuoteClassifier(),
        notion=NotionSync(object(), signals_db="", agencies_db=""),
        today=TODAY,
        ignore_cadence=True,
        send_email=True,
        sync_notion=True,
        mailer=lambda subject, body, recipient: sent.append(recipient),
    )
    errors = json.loads(store.conn.execute("SELECT errors FROM runs").fetchone()["errors"])
    assert any("Notion sync skipped" in err["message"] for err in errors)
    assert sent == ["josh.ferguson3@gmail.com"]
    store.close()


def test_nullable_enums_are_anyof_not_type_array():
    from monitor.classify import SCHEMA

    problems = []

    def walk(node, path):
        if isinstance(node, dict):
            declared = node.get("type")
            if "enum" in node and isinstance(declared, list):
                problems.append(path)
            for key, value in node.items():
                walk(value, f"{path}.{key}")
        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk(value, f"{path}[{index}]")

    walk(SCHEMA, "schema")
    assert problems == []
    for name in ("signal_type", "stance", "stance_target"):
        field = SCHEMA["properties"][name]
        branches = field["anyOf"]
        assert any(branch.get("type") == "null" for branch in branches)
        enums = [branch["enum"] for branch in branches if "enum" in branch]
        assert enums and enums[0]
        assert all(isinstance(item, str) for item in enums[0])
        assert None not in enums[0]


def test_channel_id_is_exactly_24_characters():
    from monitor.collectors.youtube import CHANNEL_ID_RE

    long_token = "UC" + ("a" * 40)
    exact = "UCabcdefghijklmnopqrstuv"
    assert len(exact) == 24
    assert CHANNEL_ID_RE.search(long_token) is None
    assert CHANNEL_ID_RE.search(f"channel {exact} tail").group(1) == exact


def test_iqm2_passes_longer_timeout_and_one_retry():
    html = (FIXTURES / "roswell_iqm2_rows.html").read_text(encoding="utf-8")

    class Response:
        def __init__(self, text, content_type="text/html"):
            self.text = text
            self.content = text.encode()
            self.headers = {"content-type": content_type}

    class RecordingHttp:
        def __init__(self):
            self.calls = []

        def get(self, url, timeout=None, retries=None, headers=None):
            self.calls.append((url, timeout, retries))
            if "FileOpen" in url:
                return Response("Agenda", "text/plain")
            return Response(html)

    http = RecordingHttp()
    Iqm2Collector().collect(
        {
            "id": "roswell-meetings",
            "url": "https://roswellcityga.iqm2.com/Citizens/Default.aspx",
            "timeout": 60,
            "retries": 1,
            "max_docs": 1,
        },
        since=date(2026, 11, 1),
        until=date(2026, 11, 30),
        http=http,
    )
    assert http.calls
    assert all(timeout == 60 and retries == 1 for _url, timeout, retries in http.calls)


def test_disabled_source_is_skipped_not_an_error(tmp_path, capsys):
    registry = load_registry(
        copy_config(
            tmp_path,
            [
                {
                    "id": "axon-news",
                    "collector": "rss",
                    "entity": "axon",
                    "url": "https://www.axon.com/newsroom/rss",
                    "cadence": "daily",
                    "enabled": False,
                    "skip_reason": "disabled, skipped (axon.com returns HTTP 403)",
                }
            ],
        )
    )
    store = Store(tmp_path / "m.sqlite")
    execute(registry, store, http=client_for({}), today=TODAY, ignore_cadence=True, send_email=False, sync_notion=False)
    errors = json.loads(store.conn.execute("SELECT errors FROM runs").fetchone()["errors"])
    output = capsys.readouterr().out
    assert errors == []
    assert "skipped axon-news:" in output
    assert "disabled, skipped" in output
    store.close()


def test_legiscan_collector_raises_without_key(monkeypatch):
    monkeypatch.delenv("LEGISCAN_API_KEY", raising=False)
    with pytest.raises(MissingKeyError) as caught:
        LegiScanCollector().collect({"id": "ga-bills", "state": "GA"}, since=TODAY, until=TODAY, http=client_for({}))
    assert "missing key, skipped" in str(caught.value)
