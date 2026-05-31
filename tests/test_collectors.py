"""Tests for collectors — shell and git TSV parsers."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from devmem.models import EventType, Source


class TestShellCollector:
    def test_parse_valid_line(self, tmp_path):
        from devmem.collectors.shell import ShellCollector

        events = []
        tsv = tmp_path / "shell.tsv"
        tsv.write_text("")

        collector = ShellCollector(
            shell_tsv=tsv,
            cmd_ignore_patterns=[],
            event_callback=events.append,
            get_offset=lambda: None,
            set_offset=lambda v: None,
        )

        line = "2026-05-21T10:00:00Z\t/home/user/myapp\tgit status\t0\t45"
        event = collector._parse_line(line)

        assert event is not None
        assert event.event_type == EventType.TERMINAL_CMD
        assert event.source == Source.SHELL_HOOK
        assert event.raw_data["cmd"] == "git status"
        assert event.raw_data["cwd"] == "/home/user/myapp"
        assert event.raw_data["exit_code"] == 0
        assert event.raw_data["duration_ms"] == 45
        assert event.date == "2026-05-21"

    def test_privacy_filter_sensitive_cmd(self, tmp_path):
        from devmem.collectors.shell import ShellCollector

        events = []
        tsv = tmp_path / "shell.tsv"
        tsv.write_text("")

        collector = ShellCollector(
            shell_tsv=tsv,
            cmd_ignore_patterns=["*password*", "*secret*"],
            event_callback=events.append,
            get_offset=lambda: None,
            set_offset=lambda v: None,
        )

        assert collector._parse_line("2026-05-21T10:00:00Z\t/cwd\techo mypassword\t0\t10") is None
        assert collector._parse_line("2026-05-21T10:00:00Z\t/cwd\tset SECRET=foo\t0\t10") is None
        # Normal command should pass
        assert collector._parse_line("2026-05-21T10:00:00Z\t/cwd\tls -la\t0\t10") is not None

    def test_parse_malformed_line(self, tmp_path):
        from devmem.collectors.shell import ShellCollector

        events = []
        tsv = tmp_path / "shell.tsv"
        tsv.write_text("")

        collector = ShellCollector(
            shell_tsv=tsv,
            cmd_ignore_patterns=[],
            event_callback=events.append,
            get_offset=lambda: None,
            set_offset=lambda v: None,
        )

        assert collector._parse_line("") is None
        assert collector._parse_line("only_one_column") is None

    def test_incremental_read(self, tmp_path):
        from devmem.collectors.shell import ShellCollector

        events = []
        tsv = tmp_path / "shell.tsv"
        tsv.write_text(
            "2026-05-21T10:00:00Z\t/cwd\tgit status\t0\t10\n"
            "2026-05-21T10:01:00Z\t/cwd\tls\t0\t5\n"
        )

        offsets = []
        collector = ShellCollector(
            shell_tsv=tsv,
            cmd_ignore_patterns=[],
            event_callback=events.append,
            get_offset=lambda: None,
            set_offset=lambda v: offsets.append(v),
        )
        collector._read_new_lines()

        assert len(events) == 2
        assert events[0].raw_data["cmd"] == "git status"
        assert events[1].raw_data["cmd"] == "ls"
        # Offset should have been stored
        assert len(offsets) > 0


class TestGitCollector:
    def test_parse_commit_line(self, tmp_path):
        from devmem.collectors.git import GitCollector

        events = []
        tsv = tmp_path / "git.tsv"
        tsv.write_text("")

        collector = GitCollector(
            git_tsv=tsv,
            event_callback=events.append,
            get_kv=lambda k: None,
            set_kv=lambda k, v: None,
        )

        line = "2026-05-21T10:05:00Z\tcommit\t/home/user/myapp\tabc123\tmain\tFix auth bug\tauth.py|token.py\tAlice"
        event = collector._parse_line(line)

        assert event is not None
        assert event.event_type == EventType.GIT_COMMIT
        assert event.raw_data["hash"] == "abc123"
        assert event.raw_data["message"] == "Fix auth bug"
        assert event.raw_data["branch"] == "main"
        assert "auth.py" in event.raw_data["files"]
        assert event.repo_name == "myapp"

    def test_parse_branch_line(self, tmp_path):
        from devmem.collectors.git import GitCollector

        events = []
        tsv = tmp_path / "git.tsv"
        tsv.write_text("")

        collector = GitCollector(
            git_tsv=tsv,
            event_callback=events.append,
            get_kv=lambda k: None,
            set_kv=lambda k, v: None,
        )

        line = "2026-05-21T10:10:00Z\tbranch\t/home/user/myapp\tmain\tfeature/new-ui"
        event = collector._parse_line(line)

        assert event is not None
        assert event.event_type == EventType.GIT_BRANCH
        assert event.raw_data["old_branch"] == "main"
        assert event.raw_data["new_branch"] == "feature/new-ui"

    def test_parse_empty_line(self, tmp_path):
        from devmem.collectors.git import GitCollector

        events = []
        tsv = tmp_path / "git.tsv"
        tsv.write_text("")

        collector = GitCollector(
            git_tsv=tsv,
            event_callback=events.append,
            get_kv=lambda k: None,
            set_kv=lambda k, v: None,
        )

        assert collector._parse_line("") is None
        assert collector._parse_line("   ") is None

    def test_parse_push_line(self, tmp_path):
        from devmem.collectors.git import GitCollector

        events = []
        tsv = tmp_path / "git.tsv"
        tsv.write_text("")

        collector = GitCollector(
            git_tsv=tsv,
            event_callback=events.append,
            get_kv=lambda k: None,
            set_kv=lambda k, v: None,
        )

        line = "2026-05-21T10:15:00Z\tpush\t/home/user/myapp\torigin\tmain\t3"
        event = collector._parse_line(line)

        assert event is not None
        assert event.event_type == EventType.GIT_PUSH
        assert event.raw_data["remote"] == "origin"
        assert event.raw_data["branch"] == "main"
        assert event.raw_data["commit_count"] == 3
        assert event.repo_name == "myapp"

    def test_parse_merge_line(self, tmp_path):
        from devmem.collectors.git import GitCollector

        events = []
        tsv = tmp_path / "git.tsv"
        tsv.write_text("")

        collector = GitCollector(
            git_tsv=tsv,
            event_callback=events.append,
            get_kv=lambda k: None,
            set_kv=lambda k, v: None,
        )

        line = "2026-05-21T10:20:00Z\tmerge\t/home/user/myapp\tmain\tfeature/new-ui\t0"
        event = collector._parse_line(line)

        assert event is not None
        assert event.event_type == EventType.GIT_MERGE
        assert event.raw_data["branch"] == "main"
        assert event.raw_data["merged_branch"] == "feature/new-ui"
        assert event.raw_data["is_squash"] is False
        assert event.repo_name == "myapp"

    def test_parse_squash_merge_line(self, tmp_path):
        from devmem.collectors.git import GitCollector

        events = []
        tsv = tmp_path / "git.tsv"
        tsv.write_text("")

        collector = GitCollector(
            git_tsv=tsv,
            event_callback=events.append,
            get_kv=lambda k: None,
            set_kv=lambda k, v: None,
        )

        line = "2026-05-21T10:21:00Z\tmerge\t/home/user/myapp\tmain\tfeature/squash\t1"
        event = collector._parse_line(line)

        assert event is not None
        assert event.raw_data["is_squash"] is True


class TestCmdCategorizer:
    def _cat(self, cmd):
        from devmem.collectors.shell import _categorize_cmd
        return _categorize_cmd(cmd)

    def test_test_commands(self):
        assert self._cat("pytest tests/") == "test"
        assert self._cat("python -m pytest") == "test"
        assert self._cat("npm test") == "test"
        assert self._cat("go test ./...") == "test"
        assert self._cat("cargo test") == "test"

    def test_build_commands(self):
        assert self._cat("make") == "build"
        assert self._cat("make -j4") == "build"
        assert self._cat("cmake ..") == "build"
        assert self._cat("cmake") == "build"
        assert self._cat("npm run build") == "build"
        assert self._cat("tsc") == "build"
        assert self._cat("cargo build") == "build"

    def test_install_commands(self):
        assert self._cat("pip install -r requirements.txt") == "install"
        assert self._cat("npm install") == "install"
        assert self._cat("brew install ripgrep") == "install"

    def test_deploy_commands(self):
        assert self._cat("docker build .") == "deploy"
        assert self._cat("docker run -it ubuntu") == "deploy"
        assert self._cat("kubectl get pods") == "deploy"
        assert self._cat("terraform apply") == "deploy"

    def test_vcs_commands(self):
        assert self._cat("git push origin main") == "vcs"
        assert self._cat("git pull") == "vcs"
        assert self._cat("git fetch --all") == "vcs"

    def test_other_commands(self):
        assert self._cat("ls -la") == "other"
        assert self._cat("echo hello") == "other"
        assert self._cat("cat README.md") == "other"

    def test_cmd_category_in_event_metadata(self, tmp_path):
        from devmem.collectors.shell import ShellCollector

        events = []
        tsv = tmp_path / "shell.tsv"
        tsv.write_text("")

        collector = ShellCollector(
            shell_tsv=tsv,
            cmd_ignore_patterns=[],
            event_callback=events.append,
            get_offset=lambda: None,
            set_offset=lambda v: None,
        )

        event = collector._parse_line("2026-05-21T10:00:00Z\t/cwd\tpytest tests/\t0\t1200")
        assert event is not None
        assert event.metadata.get("cmd_category") == "test"

        event2 = collector._parse_line("2026-05-21T10:01:00Z\t/cwd\tls -la\t0\t20")
        assert event2 is not None
        assert event2.metadata.get("cmd_category") == "other"


class TestAIChatDedup:
    def _make_collector(self):
        from devmem.collectors.ai_chat import AIChatCollector
        store = {}
        return AIChatCollector(
            event_callback=lambda e: None,
            get_kv=lambda k: store.get(k),
            set_kv=lambda k, v: store.__setitem__(k, v),
        )

    def _make_ai_event(self, timestamp, content, repo_name="repo"):
        from devmem.models import Event, EventType, Source
        return Event(
            timestamp=timestamp,
            date=timestamp[:10],
            event_type=EventType.AI_CHAT,
            source=Source.AI_CHAT_PARSER,
            content=content,
            raw_data={},
            repo_name=repo_name,
        )

    def test_exact_duplicate_is_dropped(self):
        collector = self._make_collector()
        e = self._make_ai_event("2026-05-01T10:00:00Z", "[copilot chat] user: help")
        assert collector._is_duplicate(e) is False  # first seen
        assert collector._is_duplicate(e) is True   # true duplicate (same ts + content)

    def test_same_content_different_timestamps_not_dropped(self):
        """Same message sent in two different sessions must both be recorded."""
        collector = self._make_collector()
        e1 = self._make_ai_event("2026-05-01T10:00:00Z", "[copilot chat] user: help")
        e2 = self._make_ai_event("2026-05-02T11:00:00Z", "[copilot chat] user: help")
        assert collector._is_duplicate(e1) is False
        assert collector._is_duplicate(e2) is False  # different timestamp — not a dup

    def test_same_content_same_timestamp_is_duplicate(self):
        """Re-processed entry from same file parse must be deduplicated."""
        collector = self._make_collector()
        e1 = self._make_ai_event("2026-05-01T10:00:00Z", "[copilot chat] user: refactor this")
        e2 = self._make_ai_event("2026-05-01T10:00:00Z", "[copilot chat] user: refactor this")
        assert collector._is_duplicate(e1) is False
        assert collector._is_duplicate(e2) is True
