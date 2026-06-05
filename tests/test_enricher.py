"""Tests for the event enricher."""

from __future__ import annotations

import pytest

from recall.models import Event, EventType, Source


def _make_terminal_event(cmd: str, cwd: str = "/home/user/myapp") -> Event:
    from recall.models import build_content

    raw = {"cmd": cmd, "cwd": cwd, "exit_code": 0, "duration_ms": 100}
    return Event(
        timestamp="2026-05-21T10:00:00Z",
        date="2026-05-21",
        event_type=EventType.TERMINAL_CMD,
        source=Source.SHELL_HOOK,
        content=build_content(EventType.TERMINAL_CMD, raw),
        raw_data=raw,
    )


class TestEnricher:
    def _make_enricher(self, cmd_ignore=None, file_ignore=None, repo_ignore=None):
        from recall.processor.enricher import Enricher

        repos = {}

        def upsert(path, name, ts=None, remote=None):
            repos[path] = name

        return Enricher(
            cmd_ignore_patterns=cmd_ignore or [],
            file_ignore_patterns=file_ignore or [],
            repo_ignore_patterns=repo_ignore or [],
            upsert_repo=upsert,
        ), repos

    def test_sensitive_command_dropped(self):
        enricher, _ = self._make_enricher(cmd_ignore=["*password*"])
        event = _make_terminal_event("echo mypassword123")
        result = enricher.enrich(event)
        assert result is None

    def test_normal_command_passes(self):
        enricher, _ = self._make_enricher(cmd_ignore=["*password*"])
        event = _make_terminal_event("ls -la")
        result = enricher.enrich(event)
        assert result is not None

    def test_content_rebuilt_with_repo_name(self):
        enricher, repos = self._make_enricher()
        event = _make_terminal_event("git status", cwd="/tmp")
        # No git root exists for /tmp, so repo_name stays None
        result = enricher.enrich(event)
        assert result is not None
        assert "git status" in result.content

    def test_language_detected_for_file_save(self):
        from recall.models import build_content

        enricher, _ = self._make_enricher()
        raw = {
            "filename": "app.py",
            "file_path": "/home/user/repo/app.py",
            "language": "",
            "repo_name": "repo",
        }
        event = Event(
            timestamp="2026-05-21T10:00:00Z",
            date="2026-05-21",
            event_type=EventType.FILE_SAVE,
            source=Source.VSCODE_EXT,
            content=build_content(EventType.FILE_SAVE, raw),
            raw_data=raw,
        )
        result = enricher.enrich(event)
        assert result is not None
        assert result.raw_data.get("language") == "python"

    def test_sensitive_file_dropped(self):
        from recall.models import build_content

        enricher, _ = self._make_enricher(file_ignore=["*.env"])
        raw = {"filename": ".env", "file_path": "/home/user/repo/.env", "language": ""}
        event = Event(
            timestamp="2026-05-21T10:00:00Z",
            date="2026-05-21",
            event_type=EventType.FILE_SAVE,
            source=Source.VSCODE_EXT,
            content=build_content(EventType.FILE_SAVE, raw),
            raw_data=raw,
        )
        result = enricher.enrich(event)
        assert result is None
