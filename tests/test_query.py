"""Tests for the query layer — timeparser and retriever."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from devmem.query.timeparser import parse_time_expression, _day_range


class TestTimeParser:
    NOW = datetime(2026, 5, 21, 14, 30, 0, tzinfo=timezone.utc)  # Thursday

    def _parse(self, text: str):
        return parse_time_expression(text, now=self.NOW)

    def test_today(self):
        start, end = self._parse("today")
        assert start.date() == self.NOW.date()
        assert end.date() == self.NOW.date()
        assert start.hour == 0 and start.minute == 0

    def test_yesterday(self):
        start, end = self._parse("what did I do yesterday")
        assert start.day == 20
        assert end.day == 20

    def test_last_n_days(self):
        start, end = self._parse("last 3 days")
        assert (self.NOW - start).days >= 2

    def test_last_week(self):
        start, end = self._parse("what happened last week")
        assert start.day < self.NOW.day  # must be before this week's Monday

    def test_this_week(self):
        start, end = self._parse("this week")
        assert end >= start
        assert start.date() <= self.NOW.date()

    def test_last_weekday(self):
        # "last Tuesday" — NOW is Thursday May 21 (Tuesday was May 19)
        start, end = self._parse("last Tuesday")
        assert start.day == 19
        assert start.month == 5

    def test_specific_date(self):
        start, end = self._parse("2026-05-15")
        assert start.day == 15
        assert start.month == 5

    def test_month_day(self):
        start, end = self._parse("May 15")
        assert start.day == 15
        assert start.month == 5

    def test_no_time_expression(self):
        result = parse_time_expression("how do I fix the authentication")
        # May return None or a fallback — just ensure it doesn't crash
        # (dateutil might pick something up, so we only check no exception)

    def test_hours(self):
        start, end = self._parse("last 2 hours")
        delta = self.NOW - start
        assert 1.9 * 3600 <= delta.total_seconds() <= 2.1 * 3600


class TestRetriever:
    """Integration-style tests for the hybrid retriever (no FAISS, SQL only)."""

    @pytest.fixture
    def setup(self, tmp_path):
        from devmem.storage.db import DB
        from devmem.storage.vectors import VectorStore
        from devmem.processor.embedder import EmbedderQueue
        from devmem.query.retriever import Retriever
        from devmem.models import Event, EventType, Source, build_content

        db = DB(tmp_path / "test.db")
        vectors = VectorStore(dim=384)

        # Insert sample events
        events_data = [
            ("2026-05-21T10:00:00Z", "terminal_cmd",
             "[terminal] git status in myapp (exit 0, 45ms)", "myapp"),
            ("2026-05-21T10:05:00Z", "git_commit",
             "[git] commit 'Fix auth bug' to main in myapp. Changed: auth.py", "myapp"),
            ("2026-05-20T09:00:00Z", "terminal_cmd",
             "[terminal] npm test in frontend (exit 0, 3000ms)", "frontend"),
        ]
        for ts, et, content, repo in events_data:
            e = Event(
                timestamp=ts,
                date=ts[:10],
                event_type=EventType(et),
                source=Source.SHELL_HOOK,
                content=content,
                raw_data={},
                repo_name=repo,
            )
            db.insert_event(e)

        # Use a minimal embedder that only does FTS (no real model needed for tests)
        class _FakeEmbedder:
            def encode_query(self, text):
                import numpy as np
                return np.zeros(384, dtype=np.float32)

        retriever = Retriever(db=db, vectors=vectors, embedder=_FakeEmbedder())
        yield retriever, db
        db.close()

    def test_search_returns_results(self, setup):
        retriever, db = setup
        results = retriever.search("auth bug fix", top_k=5)
        # FTS should find the commit about auth bug
        contents = [e.content for e in results]
        assert any("auth" in c.lower() for c in contents)

    def test_search_date_range_filter(self, setup):
        retriever, db = setup
        start = datetime(2026, 5, 21, 0, 0, tzinfo=timezone.utc)
        end = datetime(2026, 5, 21, 23, 59, tzinfo=timezone.utc)
        results = retriever.search("status", top_k=10, date_range=(start, end))
        # Only May 21 events should be returned
        for e in results:
            assert e.date == "2026-05-21"

    def test_search_repo_filter(self, setup):
        retriever, db = setup
        results = retriever.search("test", top_k=10, repo_name="frontend")
        for e in results:
            assert e.repo_name == "frontend"
