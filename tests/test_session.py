"""Tests for session detection."""

from __future__ import annotations

import pytest

from recall.models import Event, EventType, Source


def _make_event(ts: str, repo: str = "/home/user/myapp") -> Event:
    from recall.models import build_content

    raw = {"cmd": "git status", "cwd": repo, "exit_code": 0, "duration_ms": 10}
    return Event(
        timestamp=ts,
        date=ts[:10],
        event_type=EventType.TERMINAL_CMD,
        source=Source.SHELL_HOOK,
        content=build_content(EventType.TERMINAL_CMD, raw),
        raw_data=raw,
        repo_path=repo,
        repo_name=repo.split("/")[-1],
    )


class TestSessionDetector:
    def _make_detector(self, idle_minutes: int = 30):
        from recall.processor.session import SessionDetector

        sessions = {}

        def upsert_session(sid, start, end_time=None, repo_path=None,
                           primary_repo=None, event_count=0):
            sessions[sid] = {
                "id": sid, "start_time": start, "end_time": end_time,
                "repo_path": repo_path,
            }

        return SessionDetector(
            session_idle_minutes=idle_minutes,
            get_latest_session=lambda: None,
            upsert_session=upsert_session,
        ), sessions

    def test_first_event_creates_session(self):
        detector, sessions = self._make_detector()
        event = _make_event("2026-05-21T10:00:00Z")
        result = detector.assign(event)
        assert result.session_id is not None
        assert len(sessions) == 1

    def test_events_within_window_same_session(self):
        detector, sessions = self._make_detector(idle_minutes=30)
        e1 = _make_event("2026-05-21T10:00:00Z")
        e2 = _make_event("2026-05-21T10:15:00Z")  # 15 min later

        detector.assign(e1)
        detector.assign(e2)

        assert e1.session_id == e2.session_id

    def test_events_beyond_idle_window_new_session(self):
        detector, sessions = self._make_detector(idle_minutes=30)
        e1 = _make_event("2026-05-21T10:00:00Z")
        e2 = _make_event("2026-05-21T11:00:00Z")  # 60 min later → new session

        detector.assign(e1)
        detector.assign(e2)

        assert e1.session_id != e2.session_id
        assert len(sessions) == 2
