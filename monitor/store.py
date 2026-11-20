"""SQLite state for documents, signals, stances, snapshots, and runs."""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import date, datetime, timedelta, timezone
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
  passed_prefilter INTEGER NOT NULL DEFAULT 0,
  classified_at TEXT,
  text TEXT
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
  alpr_stance TEXT,
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
  signals_created INTEGER, errors TEXT, source_health TEXT
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
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        columns = {row[1] for row in self.conn.execute("PRAGMA table_info(documents)")}
        if "classified_at" not in columns:
            self.conn.execute("ALTER TABLE documents ADD COLUMN classified_at TEXT")
            # Already prefiltered with no hit. Do not refetch them on the next backfill.
            self.conn.execute(
                "UPDATE documents SET classified_at = fetched_at WHERE passed_prefilter = 0"
            )
        if "text" not in columns:
            self.conn.execute("ALTER TABLE documents ADD COLUMN text TEXT")
        run_columns = {row[1] for row in self.conn.execute("PRAGMA table_info(runs)")}
        if "source_health" not in run_columns:
            self.conn.execute("ALTER TABLE runs ADD COLUMN source_health TEXT")
        signal_columns = {row[1] for row in self.conn.execute("PRAGMA table_info(signals)")}
        if "alpr_stance" not in signal_columns:
            self.conn.execute("ALTER TABLE signals ADD COLUMN alpr_stance TEXT")

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
              (source_id, url, content_hash, title, doc_type, published_at, fetched_at, passed_prefilter, text)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                (doc.get("text") or "")[:200_000] or None,
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def fill_text_if_empty(self, url: str, text: str) -> sqlite3.Row | None:
        """Keep RSS text for a URL we already stored, so a later run need not refetch the article."""
        if not text:
            return None
        row = self.conn.execute("SELECT * FROM documents WHERE url = ?", (url,)).fetchone()
        if row is None:
            return None
        if not row["text"]:
            self.conn.execute(
                "UPDATE documents SET text = ? WHERE id = ?",
                (text[:200_000], row["id"]),
            )
            self.conn.commit()
            row = self.conn.execute("SELECT * FROM documents WHERE id = ?", (row["id"],)).fetchone()
        return row

    def signals_missing_notion_page(self) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                """
                SELECT * FROM signals
                WHERE notion_page_id IS NULL OR notion_page_id = ''
                ORDER BY id
                """
            )
        )

    def prior_stance(
        self,
        subject_type: str,
        subject_id: str,
        document_id: int,
        published_at: str | None,
    ) -> str | None:
        """Latest stance for this subject on a different, earlier document.

        A dated document is earlier when its published_at is before this one.
        An undated document is earlier only when it was stored first (smaller id).
        """
        if published_at:
            row = self.conn.execute(
                """
                SELECT st.stance
                FROM stances st
                JOIN signals s ON s.id = st.signal_id
                LEFT JOIN documents d ON d.id = s.document_id
                WHERE st.subject_type = ?
                  AND st.subject_id = ?
                  AND s.document_id != ?
                  AND ifnull(s.summary, '') NOT LIKE 'Stance shift:%'
                  AND (
                    (d.published_at IS NOT NULL AND d.published_at < ?)
                    OR (d.published_at IS NOT NULL AND d.published_at = ? AND s.document_id < ?)
                    OR (d.published_at IS NULL AND s.document_id < ?)
                  )
                ORDER BY coalesce(d.published_at, '') DESC, s.document_id DESC, s.id DESC
                LIMIT 1
                """,
                (
                    subject_type,
                    subject_id,
                    document_id,
                    published_at,
                    published_at,
                    document_id,
                    document_id,
                ),
            ).fetchone()
        else:
            row = self.conn.execute(
                """
                SELECT st.stance
                FROM stances st
                JOIN signals s ON s.id = st.signal_id
                WHERE st.subject_type = ?
                  AND st.subject_id = ?
                  AND s.document_id IS NOT NULL
                  AND s.document_id != ?
                  AND s.document_id < ?
                  AND ifnull(s.summary, '') NOT LIKE 'Stance shift:%'
                ORDER BY s.document_id DESC, s.id DESC
                LIMIT 1
                """,
                (subject_type, subject_id, document_id, document_id),
            ).fetchone()
        return row["stance"] if row else None

    def has_signal(self, document_id: int, signal_type: str, quote: str | None, summary: str) -> bool:
        """True when this document already has the same kind of signal.

        Quotes that contain one another count. A stance-shift row is not a
        duplicate of the statement it was derived from.
        """
        def norm(value: str | None) -> str:
            return re.sub(r"\s+", " ", (value or "")).strip().lower()

        want_summary = norm(summary)
        want_quote = norm(quote)
        rows = self.conn.execute(
            "SELECT quote, summary FROM signals WHERE document_id = ? AND signal_type = ?",
            (document_id, signal_type),
        )
        for row in rows:
            have_summary = norm(row["summary"])
            have_shift = have_summary.startswith("stance shift:")
            want_shift = want_summary.startswith("stance shift:")
            if have_shift != want_shift:
                continue
            if have_summary == want_summary:
                return True
            have_quote = norm(row["quote"])
            if not have_quote and not want_quote:
                return True
            if have_quote and want_quote and (have_quote in want_quote or want_quote in have_quote):
                return True
        return False

    def reopen_unmatched_documents(self, source_id: str) -> int:
        """Let a later backfill reconsider agency documents that never became signals."""
        cur = self.conn.execute(
            """
            UPDATE documents
            SET classified_at = NULL
            WHERE source_id = ?
              AND passed_prefilter = 0
              AND NOT EXISTS (SELECT 1 FROM signals s WHERE s.document_id = documents.id)
            """,
            (source_id,),
        )
        self.conn.commit()
        return int(cur.rowcount)

    def documents_without_signals(self, source_id: str, since: date, until: date) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                """
                SELECT d.* FROM documents d
                WHERE d.source_id = ?
                  AND d.classified_at IS NULL
                  AND NOT EXISTS (SELECT 1 FROM signals s WHERE s.document_id = d.id)
                  AND (
                    (
                      d.published_at IS NOT NULL AND d.published_at != ''
                      AND date(d.published_at) >= date(?)
                      AND date(d.published_at) <= date(?)
                    )
                    OR (
                      (d.published_at IS NULL OR d.published_at = '')
                      AND date(d.fetched_at) >= date(?)
                      AND date(d.fetched_at) <= date(?)
                    )
                  )
                """,
                (source_id, since.isoformat(), until.isoformat(), since.isoformat(), until.isoformat()),
            )
        )

    def mark_classified(self, document_id: int) -> None:
        self.conn.execute(
            "UPDATE documents SET classified_at = ? WHERE id = ?",
            (utcnow(), document_id),
        )
        self.conn.commit()

    def latest_started_at(self) -> str | None:
        row = self.conn.execute(
            "SELECT started_at FROM runs WHERE started_at IS NOT NULL ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return row["started_at"] if row else None

    def mark_prefilter(self, document_id: int, passed: bool) -> None:
        self.conn.execute(
            "UPDATE documents SET passed_prefilter = ? WHERE id = ?",
            (1 if passed else 0, document_id),
        )
        self.conn.commit()

    def document_counts_by_source(self) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT source_id, COUNT(*) AS n FROM documents GROUP BY source_id"
        )
        return {row["source_id"]: int(row["n"]) for row in rows}

    def adopt_richer_text(self, url: str, text: str) -> sqlite3.Row | None:
        """Keep a longer copy of a URL. Reopen it when it never became a signal."""
        row = self.conn.execute("SELECT * FROM documents WHERE url = ?", (url,)).fetchone()
        if row is None:
            return None
        new_text = (text or "")[:200_000]
        if len(new_text) > len(row["text"] or ""):
            self.conn.execute("UPDATE documents SET text = ? WHERE id = ?", (new_text, row["id"]))
        has_signal = self.conn.execute(
            "SELECT 1 FROM signals WHERE document_id = ?",
            (row["id"],),
        ).fetchone()
        if not has_signal:
            self.conn.execute(
                "UPDATE documents SET classified_at = NULL, passed_prefilter = 0 WHERE id = ?",
                (row["id"],),
            )
        self.conn.commit()
        return self.conn.execute("SELECT * FROM documents WHERE id = ?", (row["id"],)).fetchone()

    def signal_by_bill(self, bill_key: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM signals WHERE bill_id = ? ORDER BY id LIMIT 1",
            (bill_key,),
        ).fetchone()

    def update_signal(self, signal_id: int, signal: dict[str, Any]) -> None:
        self.conn.execute(
            """
            UPDATE signals SET
              document_id = ?, signal_type = ?, summary = ?, quote = ?, confidence = ?,
              score = ?, tier = ?, key_date = ?, key_date_meaning = ?, bill_id = ?,
              needs_review = ?, alpr_stance = ?, created_at = ?
            WHERE id = ?
            """,
            (
                signal["document_id"],
                signal["signal_type"],
                signal["summary"],
                signal.get("quote"),
                signal.get("confidence"),
                signal.get("score"),
                signal.get("tier"),
                signal.get("key_date"),
                signal.get("key_date_meaning"),
                signal.get("bill_id"),
                int(signal.get("needs_review") or 0),
                signal.get("alpr_stance"),
                utcnow(),
                signal_id,
            ),
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
              notion_page_id, alpr_stance, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
                signal.get("alpr_stance"),
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
        source_health: list[str] | None = None,
    ) -> None:
        self.conn.execute(
            """
            UPDATE runs
            SET finished_at = ?, docs_fetched = ?, docs_new = ?, passages_classified = ?,
                signals_created = ?, errors = ?, source_health = ?
            WHERE id = ?
            """,
            (
                utcnow(),
                docs_fetched,
                docs_new,
                passages_classified,
                signals_created,
                json.dumps(errors),
                json.dumps(source_health or []),
                run_id,
            ),
        )
        self.conn.commit()

    def latest_source_health(self) -> list[str]:
        row = self.conn.execute(
            """
            SELECT source_health FROM runs
            WHERE source_health IS NOT NULL AND source_health != ''
            ORDER BY id DESC LIMIT 1
            """
        ).fetchone()
        if not row or not row["source_health"]:
            return []
        try:
            loaded = json.loads(row["source_health"])
        except json.JSONDecodeError:
            return []
        return loaded if isinstance(loaded, list) else []

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
