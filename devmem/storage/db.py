"""SQLite storage — schema creation, CRUD operations, and FTS5 search."""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Generator, Optional

from devmem.models import Event, EventType, Source

# ---------------------------------------------------------------------------
# SQL DDL
# ---------------------------------------------------------------------------

_SCHEMA_SQL = """
-- Main events table
CREATE TABLE IF NOT EXISTS events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       TEXT    NOT NULL,
    date            TEXT    NOT NULL,
    event_type      TEXT    NOT NULL,
    source          TEXT    NOT NULL,
    repo_path       TEXT,
    repo_name       TEXT,
    content         TEXT    NOT NULL,
    raw_data        TEXT    NOT NULL,
    metadata        TEXT    NOT NULL DEFAULT '{}',
    session_id      TEXT,
    embedding_id    INTEGER
    -- event_type values validated at application layer via EventType enum
);

CREATE INDEX IF NOT EXISTS idx_events_date       ON events(date);
CREATE INDEX IF NOT EXISTS idx_events_timestamp  ON events(timestamp);
CREATE INDEX IF NOT EXISTS idx_events_type       ON events(event_type);
CREATE INDEX IF NOT EXISTS idx_events_repo       ON events(repo_name);
CREATE INDEX IF NOT EXISTS idx_events_session    ON events(session_id);
CREATE INDEX IF NOT EXISTS idx_events_embedding  ON events(embedding_id);

-- Full-text search (BM25 via SQLite FTS5)
CREATE VIRTUAL TABLE IF NOT EXISTS events_fts USING fts5(
    content,
    repo_name,
    content=events,
    content_rowid=id,
    tokenize='porter unicode61'
);

-- FTS sync triggers
CREATE TRIGGER IF NOT EXISTS events_ai AFTER INSERT ON events BEGIN
    INSERT INTO events_fts(rowid, content, repo_name)
    VALUES (new.id, new.content, COALESCE(new.repo_name, ''));
END;
CREATE TRIGGER IF NOT EXISTS events_ad AFTER DELETE ON events BEGIN
    INSERT INTO events_fts(events_fts, rowid, content, repo_name)
    VALUES ('delete', old.id, old.content, COALESCE(old.repo_name, ''));
END;

-- Known repos
CREATE TABLE IF NOT EXISTS repos (
    path            TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    first_seen      TEXT NOT NULL,
    last_active     TEXT,
    event_count     INTEGER DEFAULT 0,
    languages       TEXT DEFAULT '[]',
    remote_url      TEXT
);

-- Work sessions (auto-detected)
CREATE TABLE IF NOT EXISTS sessions (
    id              TEXT PRIMARY KEY,
    start_time      TEXT NOT NULL,
    end_time        TEXT,
    repo_path       TEXT,
    primary_repo    TEXT,
    event_count     INTEGER DEFAULT 0
);

-- Daily summaries (LLM-generated)
CREATE TABLE IF NOT EXISTS daily_summaries (
    date            TEXT PRIMARY KEY,
    summary         TEXT,
    repos_active    TEXT DEFAULT '[]',
    event_count     INTEGER DEFAULT 0,
    highlights      TEXT DEFAULT '[]',
    generated_at    TEXT
);

-- Key-value store for daemon state
CREATE TABLE IF NOT EXISTS kv_store (
    key     TEXT PRIMARY KEY,
    value   TEXT
);
"""


# ---------------------------------------------------------------------------
# DB class
# ---------------------------------------------------------------------------


class DB:
    """SQLite database wrapper for DevMem."""

    def __init__(self, db_path: Path) -> None:
        self._path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection = sqlite3.connect(
            str(db_path),
            check_same_thread=False,
            timeout=30,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._write_lock = threading.Lock()
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.executescript(_SCHEMA_SQL)
        self._conn.commit()
        self._migrate_remove_event_type_check()

    def _migrate_remove_event_type_check(self) -> None:
        """One-time migration: rebuild events table without the event_type CHECK constraint."""
        import logging as _logging
        _log = _logging.getLogger(__name__)
        cur = self._conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='events'"
        )
        row = cur.fetchone()
        if row is None or "CONSTRAINT valid_type CHECK" not in (row[0] or ""):
            return
        _log.info("DB migration: removing event_type CHECK constraint")
        # SQLite requires a full table rebuild to drop a CHECK constraint.
        # After rebuilding we must also recreate all indexes and FTS triggers
        # that were defined on the original table.
        self._conn.executescript("""
            ALTER TABLE events RENAME TO _events_bak;
            CREATE TABLE events (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp       TEXT    NOT NULL,
                date            TEXT    NOT NULL,
                event_type      TEXT    NOT NULL,
                source          TEXT    NOT NULL,
                repo_path       TEXT,
                repo_name       TEXT,
                content         TEXT    NOT NULL,
                raw_data        TEXT    NOT NULL,
                metadata        TEXT    NOT NULL DEFAULT '{}',
                session_id      TEXT,
                embedding_id    INTEGER
            );
            INSERT INTO events SELECT * FROM _events_bak;
            DROP TABLE _events_bak;

            -- Recreate indexes (IF NOT EXISTS is safe if they already exist)
            CREATE INDEX IF NOT EXISTS idx_events_date       ON events(date);
            CREATE INDEX IF NOT EXISTS idx_events_timestamp  ON events(timestamp);
            CREATE INDEX IF NOT EXISTS idx_events_type       ON events(event_type);
            CREATE INDEX IF NOT EXISTS idx_events_repo       ON events(repo_name);
            CREATE INDEX IF NOT EXISTS idx_events_session    ON events(session_id);
            CREATE INDEX IF NOT EXISTS idx_events_embedding  ON events(embedding_id);

            -- Recreate FTS sync triggers
            DROP TRIGGER IF EXISTS events_ai;
            CREATE TRIGGER events_ai AFTER INSERT ON events BEGIN
                INSERT INTO events_fts(rowid, content, repo_name)
                VALUES (new.id, new.content, COALESCE(new.repo_name, ''));
            END;
            DROP TRIGGER IF EXISTS events_ad;
            CREATE TRIGGER events_ad AFTER DELETE ON events BEGIN
                INSERT INTO events_fts(events_fts, rowid, content, repo_name)
                VALUES ('delete', old.id, old.content, COALESCE(old.repo_name, ''));
            END;
        """)
        _log.info("DB migration: complete")

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def _tx(self) -> Generator[sqlite3.Connection, None, None]:
        """Context manager that commits on success and rolls back on error."""
        self._write_lock.acquire()
        try:
            yield self._conn
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        finally:
            self._write_lock.release()

    # ------------------------------------------------------------------
    # Event CRUD
    # ------------------------------------------------------------------

    def insert_event(self, event: Event) -> int:
        """Insert a single event and return the row id."""
        row = event.to_db_row()
        with self._tx() as conn:
            cur = conn.execute(
                """
                INSERT INTO events
                    (timestamp, date, event_type, source, repo_path, repo_name,
                     content, raw_data, metadata, session_id, embedding_id)
                VALUES
                    (:timestamp, :date, :event_type, :source, :repo_path, :repo_name,
                     :content, :raw_data, :metadata, :session_id, :embedding_id)
                """,
                row,
            )
            row_id: int = cur.lastrowid  # type: ignore[assignment]
        return row_id

    def insert_events_batch(self, events: list[Event]) -> list[int]:
        """Insert multiple events and return their row ids."""
        ids: list[int] = []
        with self._tx() as conn:
            for event in events:
                row = event.to_db_row()
                cur = conn.execute(
                    """
                    INSERT INTO events
                        (timestamp, date, event_type, source, repo_path, repo_name,
                         content, raw_data, metadata, session_id, embedding_id)
                    VALUES
                        (:timestamp, :date, :event_type, :source, :repo_path, :repo_name,
                         :content, :raw_data, :metadata, :session_id, :embedding_id)
                    """,
                    row,
                )
                ids.append(cur.lastrowid)  # type: ignore[arg-type]
        return ids

    def update_embedding_id(self, event_id: int, embedding_id: int) -> None:
        with self._tx() as conn:
            conn.execute(
                "UPDATE events SET embedding_id = ? WHERE id = ?",
                (embedding_id, event_id),
            )

    def get_event_by_id(self, event_id: int) -> Optional[Event]:
        cur = self._conn.execute("SELECT * FROM events WHERE id = ?", (event_id,))
        row = cur.fetchone()
        return Event.from_db_row(dict(row)) if row else None

    def get_events_by_ids(self, event_ids: list[int]) -> list[Event]:
        if not event_ids:
            return []
        placeholders = ",".join("?" * len(event_ids))
        cur = self._conn.execute(
            f"SELECT * FROM events WHERE id IN ({placeholders}) ORDER BY timestamp",
            event_ids,
        )
        return [Event.from_db_row(dict(r)) for r in cur.fetchall()]

    def get_events_by_date(self, date: str) -> list[Event]:
        cur = self._conn.execute(
            "SELECT * FROM events WHERE date = ? ORDER BY timestamp",
            (date,),
        )
        return [Event.from_db_row(dict(r)) for r in cur.fetchall()]

    def get_events_by_date_range(self, start: str, end: str) -> list[Event]:
        """Return events where timestamp is in [start, end] (ISO 8601 strings)."""
        cur = self._conn.execute(
            "SELECT * FROM events WHERE timestamp >= ? AND timestamp <= ? ORDER BY timestamp",
            (start, end),
        )
        return [Event.from_db_row(dict(r)) for r in cur.fetchall()]

    def get_events_by_embedding_ids(self, embedding_ids: list[int]) -> list[Event]:
        if not embedding_ids:
            return []
        placeholders = ",".join("?" * len(embedding_ids))
        cur = self._conn.execute(
            f"SELECT * FROM events WHERE embedding_id IN ({placeholders})",
            embedding_ids,
        )
        return [Event.from_db_row(dict(r)) for r in cur.fetchall()]

    def get_events_by_filters(
        self,
        date_range: Optional[tuple[str, str]] = None,
        event_types: Optional[list[EventType]] = None,
        repo_name: Optional[str] = None,
        limit: int = 1000,
    ) -> list[Event]:
        """Return events matching all provided filters."""
        clauses: list[str] = []
        params: list = []

        if date_range:
            clauses.append("timestamp >= ? AND timestamp <= ?")
            params.extend(date_range)
        if event_types:
            phs = ",".join("?" * len(event_types))
            clauses.append(f"event_type IN ({phs})")
            params.extend(et.value for et in event_types)
        if repo_name:
            clauses.append("repo_name = ?")
            params.append(repo_name)

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"SELECT * FROM events {where} ORDER BY timestamp LIMIT ?"
        params.append(limit)

        cur = self._conn.execute(sql, params)
        return [Event.from_db_row(dict(r)) for r in cur.fetchall()]

    def get_events_unembedded(self, limit: int = 100) -> list[Event]:
        """Return events that have not yet been embedded."""
        cur = self._conn.execute(
            "SELECT * FROM events WHERE embedding_id IS NULL ORDER BY id LIMIT ?",
            (limit,),
        )
        return [Event.from_db_row(dict(r)) for r in cur.fetchall()]

    def delete_events_before(self, date: str) -> int:
        """Delete events with date < *date* and return the number deleted."""
        with self._tx() as conn:
            cur = conn.execute("DELETE FROM events WHERE date < ?", (date,))
            return cur.rowcount

    def get_event_count(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) FROM events").fetchone()
        return row[0] if row else 0

    def get_event_counts_by_type(self) -> dict[str, int]:
        cur = self._conn.execute(
            "SELECT event_type, COUNT(*) as cnt FROM events GROUP BY event_type"
        )
        return {r["event_type"]: r["cnt"] for r in cur.fetchall()}

    def get_events_per_day(self, last_n_days: int = 7) -> list[dict]:
        cur = self._conn.execute(
            """
            SELECT date, COUNT(*) as cnt
            FROM events
            GROUP BY date
            ORDER BY date DESC
            LIMIT ?
            """,
            (last_n_days,),
        )
        return [dict(r) for r in cur.fetchall()]

    # ------------------------------------------------------------------
    # FTS5 search
    # ------------------------------------------------------------------

    def fts_search(self, query: str, limit: int = 50) -> list[tuple[int, float]]:
        """BM25 full-text search. Returns [(event_id, rank)] sorted by relevance."""
        # Sanitize: escape special FTS5 characters in the user query
        safe_query = _escape_fts_query(query)
        if not safe_query:
            return []
        try:
            cur = self._conn.execute(
                """
                SELECT rowid, rank
                FROM events_fts
                WHERE events_fts MATCH ?
                ORDER BY rank
                LIMIT ?
                """,
                (safe_query, limit),
            )
            # FTS5 rank is negative (more negative = better). Negate for easier use.
            return [(int(r["rowid"]), -float(r["rank"])) for r in cur.fetchall()]
        except sqlite3.OperationalError:
            # If the query is still malformed after sanitisation, return empty
            return []

    # ------------------------------------------------------------------
    # Repos
    # ------------------------------------------------------------------

    def upsert_repo(
        self,
        path: str,
        name: str,
        timestamp: Optional[str] = None,
        remote_url: Optional[str] = None,
    ) -> None:
        from datetime import datetime, timezone

        now = timestamp or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO repos (path, name, first_seen, last_active, event_count, remote_url)
                VALUES (?, ?, ?, ?, 1, ?)
                ON CONFLICT(path) DO UPDATE SET
                    last_active = excluded.last_active,
                    event_count = event_count + 1,
                    remote_url  = COALESCE(excluded.remote_url, remote_url)
                """,
                (path, name, now, now, remote_url),
            )

    def get_all_repos(self) -> list[dict]:
        cur = self._conn.execute("SELECT * FROM repos ORDER BY last_active DESC")
        return [dict(r) for r in cur.fetchall()]

    # ------------------------------------------------------------------
    # Sessions
    # ------------------------------------------------------------------

    def upsert_session(
        self,
        session_id: str,
        start_time: str,
        end_time: Optional[str] = None,
        repo_path: Optional[str] = None,
        primary_repo: Optional[str] = None,
        event_count: int = 1,
    ) -> None:
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO sessions (id, start_time, end_time, repo_path, primary_repo, event_count)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    end_time    = COALESCE(excluded.end_time, end_time),
                    event_count = event_count + 1
                """,
                (session_id, start_time, end_time, repo_path, primary_repo, event_count),
            )

    def get_latest_session(self) -> Optional[dict]:
        cur = self._conn.execute(
            "SELECT * FROM sessions ORDER BY start_time DESC LIMIT 1"
        )
        row = cur.fetchone()
        return dict(row) if row else None

    def get_latest_event(self) -> Optional[Event]:
        cur = self._conn.execute(
            "SELECT * FROM events ORDER BY timestamp DESC LIMIT 1"
        )
        row = cur.fetchone()
        return Event.from_db_row(dict(row)) if row else None

    # ------------------------------------------------------------------
    # Daily summaries
    # ------------------------------------------------------------------

    def get_daily_summary(self, date: str) -> Optional[dict]:
        cur = self._conn.execute(
            "SELECT * FROM daily_summaries WHERE date = ?", (date,)
        )
        row = cur.fetchone()
        if not row:
            return None
        d = dict(row)
        d["repos_active"] = json.loads(d["repos_active"] or "[]")
        d["highlights"] = json.loads(d["highlights"] or "[]")
        return d

    def upsert_daily_summary(
        self,
        date: str,
        summary: str,
        repos: list[str],
        highlights: list[str],
        event_count: int = 0,
    ) -> None:
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO daily_summaries (date, summary, repos_active, event_count, highlights, generated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(date) DO UPDATE SET
                    summary      = excluded.summary,
                    repos_active = excluded.repos_active,
                    event_count  = excluded.event_count,
                    highlights   = excluded.highlights,
                    generated_at = excluded.generated_at
                """,
                (date, summary, json.dumps(repos), event_count, json.dumps(highlights), now),
            )

    # ------------------------------------------------------------------
    # KV store
    # ------------------------------------------------------------------

    def get_kv(self, key: str) -> Optional[str]:
        cur = self._conn.execute("SELECT value FROM kv_store WHERE key = ?", (key,))
        row = cur.fetchone()
        return row["value"] if row else None

    def set_kv(self, key: str, value: str) -> None:
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO kv_store (key, value) VALUES (?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def delete_kv(self, key: str) -> None:
        with self._tx() as conn:
            conn.execute("DELETE FROM kv_store WHERE key = ?", (key,))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _escape_fts_query(query: str) -> str:
    """
    Produce a safe FTS5 MATCH query from a free-text string.

    We tokenise the query into words and wrap each with double-quotes so
    FTS5 treats them as phrase prefixes rather than operators.
    Special characters are stripped from each token.
    """
    import re

    tokens = re.findall(r'[\w]+', query, re.UNICODE)
    if not tokens:
        return ""
    # Quote each token to avoid FTS5 operator interpretation
    safe_tokens = [f'"{t}"' for t in tokens if t]
    return " ".join(safe_tokens)
