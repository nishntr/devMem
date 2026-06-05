"""Tests for SQLite storage layer."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from recall.models import Event, EventType, Source


def _make_event(
    event_type: EventType = EventType.TERMINAL_CMD,
    timestamp: str = "2026-05-21T10:00:00Z",
    repo_name: str = "myapp",
    content: str = "[terminal] git status in myapp (exit 0, 45ms)",
) -> Event:
    return Event(
        timestamp=timestamp,
        date=timestamp[:10],
        event_type=event_type,
        source=Source.SHELL_HOOK,
        content=content,
        raw_data={"cmd": "git status"},
        repo_name=repo_name,
    )


class TestDB:
    @pytest.fixture
    def db(self, tmp_path):
        from recall.storage.db import DB

        d = DB(tmp_path / "test.db")
        yield d
        d.close()

    def test_insert_and_retrieve_event(self, db):
        event = _make_event()
        event_id = db.insert_event(event)
        assert event_id > 0

        retrieved = db.get_event_by_id(event_id)
        assert retrieved is not None
        assert retrieved.content == event.content
        assert retrieved.event_type == EventType.TERMINAL_CMD

    def test_insert_batch(self, db):
        events = [_make_event(content=f"event {i}") for i in range(5)]
        ids = db.insert_events_batch(events)
        assert len(ids) == 5
        assert all(isinstance(i, int) for i in ids)

    def test_get_events_by_date(self, db):
        e1 = _make_event(timestamp="2026-05-21T10:00:00Z")
        e2 = _make_event(timestamp="2026-05-21T12:00:00Z")
        e3 = _make_event(timestamp="2026-05-22T10:00:00Z")
        db.insert_events_batch([e1, e2, e3])

        day_events = db.get_events_by_date("2026-05-21")
        assert len(day_events) == 2

    def test_get_events_by_date_range(self, db):
        e1 = _make_event(timestamp="2026-05-20T10:00:00Z")
        e2 = _make_event(timestamp="2026-05-21T10:00:00Z")
        e3 = _make_event(timestamp="2026-05-22T10:00:00Z")
        db.insert_events_batch([e1, e2, e3])

        ranged = db.get_events_by_date_range(
            "2026-05-20T00:00:00Z", "2026-05-21T23:59:59Z"
        )
        assert len(ranged) == 2

    def test_fts_search(self, db):
        e1 = _make_event(content="[terminal] git status in myapp (exit 0, 45ms)")
        e2 = _make_event(content="[git] commit 'Fix auth bug' to main in myapp")
        db.insert_events_batch([e1, e2])

        results = db.fts_search("auth bug")
        assert len(results) > 0
        event_id, rank = results[0]
        assert isinstance(event_id, int)
        assert isinstance(rank, float)

    def test_update_embedding_id(self, db):
        event = _make_event()
        event_id = db.insert_event(event)
        db.update_embedding_id(event_id, 999)
        retrieved = db.get_event_by_id(event_id)
        assert retrieved.embedding_id == 999

    def test_kv_store(self, db):
        db.set_kv("foo", "bar")
        assert db.get_kv("foo") == "bar"
        db.set_kv("foo", "baz")  # update
        assert db.get_kv("foo") == "baz"
        assert db.get_kv("nonexistent") is None

    def test_upsert_repo(self, db):
        db.upsert_repo("/home/user/myapp", "myapp", timestamp="2026-05-21T10:00:00Z")
        repos = db.get_all_repos()
        assert len(repos) == 1
        assert repos[0]["name"] == "myapp"
        # Upsert again — event_count should increase
        db.upsert_repo("/home/user/myapp", "myapp", timestamp="2026-05-21T11:00:00Z")
        repos = db.get_all_repos()
        assert repos[0]["event_count"] == 2

    def test_delete_events_before(self, db):
        e1 = _make_event(timestamp="2026-05-01T10:00:00Z")
        e2 = _make_event(timestamp="2026-05-21T10:00:00Z")
        db.insert_events_batch([e1, e2])
        deleted = db.delete_events_before("2026-05-10")
        assert deleted == 1
        assert db.get_event_count() == 1

    def test_daily_summary_upsert(self, db):
        db.upsert_daily_summary(
            "2026-05-21",
            "Worked on auth and UI",
            ["myapp"],
            ["Fix auth bug"],
            event_count=10,
        )
        summary = db.get_daily_summary("2026-05-21")
        assert summary is not None
        assert summary["summary"] == "Worked on auth and UI"
        assert "myapp" in summary["repos_active"]


class TestDBMigration:
    """Tests for the one-time migration that removes the event_type CHECK constraint."""

    def test_migration_preserves_indexes_and_triggers(self, tmp_path):
        """After migrating an old-style DB, indexes and FTS triggers must still exist."""
        import sqlite3
        from recall.storage.db import DB

        db_path = tmp_path / "legacy.db"

        # Build an old-style database with the CHECK constraint
        conn = sqlite3.connect(db_path)
        conn.executescript("""
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
                embedding_id    INTEGER,
                CONSTRAINT valid_type CHECK (event_type IN ('terminal_cmd'))
            );
            INSERT INTO events (timestamp, date, event_type, source, content, raw_data)
            VALUES ('2026-01-01T00:00:00Z', '2026-01-01', 'terminal_cmd', 'shell_hook',
                    'old event', '{}');
            CREATE INDEX idx_events_date ON events(date);
            CREATE INDEX idx_events_timestamp ON events(timestamp);
            CREATE INDEX idx_events_type ON events(event_type);
            CREATE INDEX idx_events_repo ON events(repo_name);
            CREATE INDEX idx_events_session ON events(session_id);
            CREATE INDEX idx_events_embedding ON events(embedding_id);
            CREATE VIRTUAL TABLE events_fts USING fts5(
                content, repo_name, content=events, content_rowid=id,
                tokenize='porter unicode61'
            );
            CREATE TRIGGER events_ai AFTER INSERT ON events BEGIN
                INSERT INTO events_fts(rowid, content, repo_name)
                VALUES (new.id, new.content, COALESCE(new.repo_name, ''));
            END;
            CREATE TRIGGER events_ad AFTER DELETE ON events BEGIN
                INSERT INTO events_fts(events_fts, rowid, content, repo_name)
                VALUES ('delete', old.id, old.content, COALESCE(old.repo_name, ''));
            END;
        """)
        conn.commit()
        conn.close()

        # Run the migration by opening with DB()
        db = DB(db_path)

        # Verify the old row survived
        events = db.get_events_by_date("2026-01-01")
        assert len(events) == 1
        assert events[0].content == "old event"

        # Verify new event types are now accepted (CHECK constraint gone)
        from recall.models import Event, EventType, Source
        new_event = Event(
            timestamp="2026-05-01T00:00:00Z",
            date="2026-05-01",
            event_type=EventType.GIT_PUSH,
            source=Source.GIT_HOOK,
            content="[git] pushed 1 commit(s) on main to origin in myapp",
            raw_data={},
        )
        eid = db.insert_event(new_event)
        assert eid > 0

        # Verify indexes survived the migration
        conn2 = sqlite3.connect(db_path)
        indexes = {
            row[0]
            for row in conn2.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='events'"
            ).fetchall()
        }
        conn2.close()
        expected_indexes = {
            "idx_events_date", "idx_events_timestamp", "idx_events_type",
            "idx_events_repo", "idx_events_session", "idx_events_embedding",
        }
        assert expected_indexes.issubset(indexes), f"Missing indexes: {expected_indexes - indexes}"

        # Verify FTS sync trigger is alive: FTS must find the new event
        results = db.fts_search("pushed commit")
        assert len(results) > 0

        db.close()

    def test_new_db_not_migrated(self, tmp_path):
        """A fresh database must not trigger the migration path."""
        import sqlite3
        from recall.storage.db import DB

        db_path = tmp_path / "fresh.db"
        db = DB(db_path)

        # schema should have no CHECK constraint on event_type
        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='events'"
        ).fetchone()
        conn.close()
        assert "CONSTRAINT valid_type CHECK" not in (row[0] or "")

        db.close()
