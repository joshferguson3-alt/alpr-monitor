"""SQLite state for documents, signals, stances, snapshots, and runs."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
  id INTEGER PRIMARY KEY,
  source_id TEXT NOT NULL,
  url TEXT NOT NULL UNIQUE,
  content_hash TEXT NOT NULL,
  title TEXT,
  doc_type TEXT,
  published_at TEXT,
  fetched_at TEXT NOT NULL,
  passed_prefilter INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS signals (
  id INTEGER PRIMARY KEY,
  document_id INTEGER NOT NULL REFERENCES documents(id),
  signal_type TEXT NOT NULL,
  summary TEXT NOT NULL,
  agency_id TEXT,
  agency_name_raw TEXT,
  person_id TEXT,
  speaker_name_raw TEXT,
  speaker_role TEXT,
  organization_id TEXT,
  vendors TEXT,
  stance TEXT,
  stance_target TEXT,
  resident_support INTEGER,
  resident_oppose INTEGER,
  key_date TEXT,
  key_date_meaning TEXT,
  dollar_amount REAL,
  camera_count INTEGER,
  bill_id TEXT,
  quote TEXT,
  confidence REAL,
  score INTEGER,
  tier TEXT,
  needs_review INTEGER NOT NULL DEFAULT 0,
  notion_page_id TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS stances (
  id INTEGER PRIMARY KEY,
  subject_type TEXT NOT NULL,
  subject_id TEXT NOT NULL,
  stance TEXT NOT NULL,
  signal_id INTEGER NOT NULL REFERENCES signals(id),
  recorded_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS snapshots (
  id INTEGER PRIMARY KEY,
  source_id TEXT NOT NULL,
  captured_at TEXT NOT NULL,
  data TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY,
  started_at TEXT, finished_at TEXT,
  docs_fetched INTEGER, docs_new INTEGER, passages_classified INTEGER,
  signals_created INTEGER, errors TEXT
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def seen(self, url: str, content_hash: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM documents WHERE url = ? OR content_hash = ?",
            (url, content_hash),
        ).fetchone()
        return row is not None

    def source_document_count(self, source_id: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM documents WHERE source_id = ?",
            (source_id,),
        ).fetchone()
        return int(row["n"])

    def insert_document(self, doc: dict[str, Any]) -> int | None:
        if self.seen(doc["url"], doc["content_hash"]):
            return None
        cur = self.conn.execute(
            """
            INSERT INTO documents
              (source_id, url, content_hash, title, doc_type, published_at, fetched_at, passed_prefilter)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                doc["source_id"],
                doc["url"],
                doc["content_hash"],
                doc.get("title"),
                doc.get("doc_type"),
                doc.get("published_at"),
                doc.get("fetched_at") or utcnow(),
                int(doc.get("passed_prefilter") or 0),
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def mark_prefilter(self, document_id: int, passed: bool) -> None:
        self.conn.execute(
            "UPDATE documents SET passed_prefilter = ? WHERE id = ?",
            (1 if passed else 0, document_id),
        )
        self.conn.commit()

    def insert_signal(self, signal: dict[str, Any]) -> int:
        cur = self.conn.execute(
            """
            INSERT INTO signals (
              document_id, signal_type, summary, agency_id, agency_name_raw, person_id,
              speaker_name_raw, speaker_role, organization_id, vendors, stance, stance_target,
              resident_support, resident_oppose, key_date, key_date_meaning, dollar_amount,
              camera_count, bill_id, quote, confidence, score, tier, needs_review,
              notion_page_id, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                signal["document_id"],
                signal["signal_type"],
                signal["summary"],
                signal.get("agency_id"),
                signal.get("agency_name_raw"),
                signal.get("person_id"),
                signal.get("speaker_name_raw"),
                signal.get("speaker_role"),
                signal.get("organization_id"),
                json.dumps(signal.get("vendors") or []),
                signal.get("stance"),
                signal.get("stance_target"),
                signal.get("resident_support"),
                signal.get("resident_oppose"),
                signal.get("key_date"),
                signal.get("key_date_meaning"),
                signal.get("dollar_amount"),
                signal.get("camera_count"),
                signal.get("bill_id"),
                signal.get("quote"),
                signal.get("confidence"),
                signal.get("score"),
                signal.get("tier"),
                int(signal.get("needs_review") or 0),
                signal.get("notion_page_id"),
                signal.get("created_at") or utcnow(),
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def set_notion_page(self, signal_id: int, page_id: str) -> None:
        self.conn.execute(
            "UPDATE signals SET notion_page_id = ? WHERE id = ?",
            (page_id, signal_id),
        )
        self.conn.commit()

    def last_stance(self, subject_type: str, subject_id: str) -> str | None:
        row = self.conn.execute(
            """
            SELECT stance FROM stances
            WHERE subject_type = ? AND subject_id = ?
            ORDER BY id DESC LIMIT 1
            """,
            (subject_type, subject_id),
        ).fetchone()
        return row["stance"] if row else None

    def insert_stance(self, subject_type: str, subject_id: str, stance: str, signal_id: int) -> None:
        self.conn.execute(
            """
            INSERT INTO stances (subject_type, subject_id, stance, signal_id, recorded_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (subject_type, subject_id, stance, signal_id, utcnow()),
        )
        self.conn.commit()

    def latest_snapshot(self, source_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT data, captured_at FROM snapshots
            WHERE source_id = ? ORDER BY id DESC LIMIT 1
            """,
            (source_id,),
        ).fetchone()
        if not row:
            return None
        return {"captured_at": row["captured_at"], "data": json.loads(row["data"])}

    def insert_snapshot(self, source_id: str, data: dict[str, Any]) -> None:
        self.conn.execute(
            "INSERT INTO snapshots (source_id, captured_at, data) VALUES (?, ?, ?)",
            (source_id, utcnow(), json.dumps(data, sort_keys=True)),
        )
        self.conn.commit()

    def start_run(self) -> int:
        cur = self.conn.execute(
            "INSERT INTO runs (started_at, docs_fetched, docs_new, passages_classified, signals_created, errors) VALUES (?, 0, 0, 0, 0, '[]')",
            (utcnow(),),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def finish_run(
        self,
        run_id: int,
        *,
        docs_fetched: int,
        docs_new: int,
        passages_classified: int,
        signals_created: int,
        errors: list[dict[str, str]],
    ) -> None:
        self.conn.execute(
            """
            UPDATE runs
            SET finished_at = ?, docs_fetched = ?, docs_new = ?, passages_classified = ?,
                signals_created = ?, errors = ?
            WHERE id = ?
            """,
            (
                utcnow(),
                docs_fetched,
                docs_new,
                passages_classified,
                signals_created,
                json.dumps(errors),
                run_id,
            ),
        )
        self.conn.commit()

    def signals_created_since(self, iso_timestamp: str) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM signals WHERE created_at >= ? ORDER BY score DESC, id ASC",
                (iso_timestamp,),
            )
        )

    def recent_signals(self, days: int) -> list[sqlite3.Row]:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).replace(microsecond=0).isoformat()
        return list(
            self.conn.execute(
                "SELECT * FROM signals WHERE created_at >= ? ORDER BY created_at ASC",
                (cutoff,),
            )
        )

    def document(self, document_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM documents WHERE id = ?", (document_id,)).fetchone()

    def passed_agendas(self, source_entity_ids: list[str] | None = None) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                """
                SELECT * FROM documents
                WHERE passed_prefilter = 1 AND doc_type IN ('agenda', 'minutes', 'packet')
                """
            )
        )
