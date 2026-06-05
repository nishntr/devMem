"""Shell collector — reads shell.tsv written by the zsh/bash hooks."""

from __future__ import annotations

import fnmatch
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from watchdog.events import FileModifiedEvent, FileSystemEventHandler
from watchdog.observers import Observer

from recall.models import Event, EventType, Source, build_content

logger = logging.getLogger(__name__)

# TSV column indices
_COL_TS = 0
_COL_CWD = 1
_COL_CMD = 2
_COL_EXIT = 3
_COL_DUR = 4

# KV key to persist read offset across daemon restarts
_OFFSET_KEY = "shell_tsv_offset"

# Command category prefixes — first matching category wins
# Each entry is (prefix_lowercase, category)
_CMD_CATEGORY_RULES: list[tuple[str, str]] = [
    # test
    ("pytest", "test"),
    ("python -m pytest", "test"),
    ("python3 -m pytest", "test"),
    ("jest ", "test"),
    ("vitest", "test"),
    ("mocha ", "test"),
    ("go test", "test"),
    ("cargo test", "test"),
    ("npm test", "test"),
    ("yarn test", "test"),
    ("dotnet test", "test"),
    ("phpunit", "test"),
    ("rspec ", "test"),
    ("flutter test", "test"),
    # build
    ("make", "build"),
    ("cmake", "build"),
    ("cargo build", "build"),
    ("cargo b ", "build"),
    ("gradle ", "build"),
    ("mvn compile", "build"),
    ("mvn package", "build"),
    ("mvn install", "build"),
    ("ant ", "build"),
    ("bazel build", "build"),
    ("ninja", "build"),
    ("msbuild", "build"),
    ("dotnet build", "build"),
    ("npm run build", "build"),
    ("yarn build", "build"),
    ("tsc ", "build"),
    ("tsc", "build"),
    ("go build", "build"),
    ("javac ", "build"),
    ("gcc ", "build"),
    ("g++ ", "build"),
    ("clang ", "build"),
    # install
    ("pip install", "install"),
    ("pip3 install", "install"),
    ("npm install", "install"),
    ("npm i ", "install"),
    ("yarn add", "install"),
    ("yarn install", "install"),
    ("brew install", "install"),
    ("apt-get install", "install"),
    ("apt install", "install"),
    ("cargo add", "install"),
    ("go get", "install"),
    ("gem install", "install"),
    ("composer install", "install"),
    ("composer require", "install"),
    ("poetry add", "install"),
    ("uv add", "install"),
    ("uv pip install", "install"),
    # deploy
    ("docker", "deploy"),
    ("kubectl ", "deploy"),
    ("terraform ", "deploy"),
    ("helm ", "deploy"),
    ("ansible", "deploy"),
    ("fly deploy", "deploy"),
    ("vercel ", "deploy"),
    ("netlify deploy", "deploy"),
    # version control
    ("git push", "vcs"),
    ("git pull", "vcs"),
    ("git fetch", "vcs"),
    ("git merge", "vcs"),
    ("git rebase", "vcs"),
    ("git clone", "vcs"),
    ("git stash", "vcs"),
    ("git cherry-pick", "vcs"),
]


class ShellCollector:
    """
    Watches shell.tsv for new lines and emits terminal_cmd Events.

    The collector is started inside the daemon and calls *event_callback*
    for each new event parsed from the TSV file.
    """

    def __init__(
        self,
        shell_tsv: Path,
        cmd_ignore_patterns: list[str],
        event_callback: Callable[[Event], None],
        get_offset: Callable[[], Optional[str]],
        set_offset: Callable[[str], None],
    ) -> None:
        self._path = shell_tsv
        self._ignore_patterns = [p.lower() for p in cmd_ignore_patterns]
        self._callback = event_callback
        self._get_offset = get_offset
        self._set_offset = set_offset

        # Restore byte offset from KV store so we don't re-process old lines
        stored = get_offset()
        self._offset: int = int(stored) if stored else 0

        self._observer: Optional[Observer] = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the watchdog observer."""
        # Ensure the file exists
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.touch(exist_ok=True)

        # If the file grew while the daemon was stopped, catch up now
        self._read_new_lines()

        handler = _ShellTSVHandler(self._path, self._on_file_change)
        self._observer = Observer()
        self._observer.schedule(handler, str(self._path.parent), recursive=False)
        self._observer.start()
        logger.info("ShellCollector watching %s", self._path)

    def stop(self) -> None:
        if self._observer:
            self._observer.stop()
            self._observer.join(timeout=5)
        logger.info("ShellCollector stopped")

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _on_file_change(self) -> None:
        with self._lock:
            self._read_new_lines()

    def _read_new_lines(self) -> None:
        """Read any new bytes from shell.tsv since last offset."""
        try:
            file_size = self._path.stat().st_size
        except OSError:
            return

        if file_size <= self._offset:
            # File was truncated/rotated — reset
            if file_size < self._offset:
                logger.warning("shell.tsv shrank; resetting offset")
                self._offset = 0
            return

        try:
            with self._path.open("rb") as fh:
                fh.seek(self._offset)
                new_bytes = fh.read()
                new_offset = fh.tell()
        except OSError as exc:
            logger.error("Cannot read shell.tsv: %s", exc)
            return

        events: list[Event] = []
        for raw_line in new_bytes.decode("utf-8", errors="replace").splitlines():
            event = self._parse_line(raw_line.rstrip("\r\n"))
            if event:
                events.append(event)

        self._offset = new_offset
        self._set_offset(str(new_offset))

        for event in events:
            try:
                self._callback(event)
            except Exception:
                logger.exception("Error in shell event callback")

    def _parse_line(self, line: str) -> Optional[Event]:
        """Parse a single TSV line into an Event, or None if invalid/filtered."""
        if not line.strip():
            return None

        parts = line.split("\t")
        if len(parts) < 5:
            logger.debug("Skipping malformed shell.tsv line: %r", line[:100])
            return None

        ts_str = parts[_COL_TS]
        cwd = parts[_COL_CWD]
        cmd = parts[_COL_CMD]

        try:
            exit_code = int(parts[_COL_EXIT])
        except ValueError:
            exit_code = 0

        try:
            duration_ms = int(parts[_COL_DUR])
        except ValueError:
            duration_ms = 0

        # Privacy: skip commands matching ignore patterns
        if self._is_sensitive(cmd):
            logger.debug("Skipping sensitive command (pattern match)")
            return None

        # Parse timestamp → date
        try:
            dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            date_str = dt.strftime("%Y-%m-%d")
        except ValueError:
            # Fallback to local now
            dt = datetime.now(timezone.utc)
            date_str = dt.strftime("%Y-%m-%d")
            ts_str = dt.strftime("%Y-%m-%dT%H:%M:%SZ")

        raw = {
            "cmd": cmd,
            "cwd": cwd,
            "exit_code": exit_code,
            "duration_ms": duration_ms,
            "repo_name": os.path.basename(cwd.rstrip("/")) if cwd else "",
        }
        content = build_content(EventType.TERMINAL_CMD, raw)

        metadata = {"cmd_category": _categorize_cmd(cmd)}

        return Event(
            timestamp=ts_str,
            date=date_str,
            event_type=EventType.TERMINAL_CMD,
            source=Source.SHELL_HOOK,
            content=content,
            raw_data=raw,
            metadata=metadata,
        )

    def _is_sensitive(self, cmd: str) -> bool:
        """Return True if the command matches any privacy ignore pattern."""
        cmd_lower = cmd.lower()
        for pattern in self._ignore_patterns:
            if fnmatch.fnmatch(cmd_lower, pattern):
                return True
        return False


# ---------------------------------------------------------------------------
# Watchdog handler
# ---------------------------------------------------------------------------


class _ShellTSVHandler(FileSystemEventHandler):
    def __init__(self, path: Path, callback: Callable[[], None]) -> None:
        self._path = str(path)
        self._callback = callback

    def on_modified(self, event: FileModifiedEvent) -> None:
        if not event.is_directory and event.src_path == self._path:
            self._callback()


def _categorize_cmd(cmd: str) -> str:
    """Return a broad category for a shell command string."""
    cmd_lower = cmd.lower().strip()
    for prefix, category in _CMD_CATEGORY_RULES:
        if cmd_lower.startswith(prefix):
            return category
    return "other"
