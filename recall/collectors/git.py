"""Git collector — reads git.tsv and provides a fallback git log poller."""

from __future__ import annotations

import fnmatch
import logging
import os
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from watchdog.events import FileModifiedEvent, FileSystemEventHandler
from watchdog.observers import Observer

from recall.models import Event, EventType, Source, build_content

logger = logging.getLogger(__name__)

# TSV column layouts
# commit: ts | "commit" | repo_path | hash | branch | message | files (|sep) | author
# branch: ts | "branch" | repo_path | old_branch | new_branch
# push:   ts | "push"   | repo_path | remote | branch | commit_count
# merge:  ts | "merge"  | repo_path | branch | merged_branch | is_squash
_COMMIT_COLS = 8
_BRANCH_COLS = 5
_PUSH_COLS = 6
_MERGE_COLS = 6

_OFFSET_KEY = "git_tsv_offset"
_LAST_POLL_KEY = "git_poller_last_run"

# Maximum directory depth when scanning for git repos
_MAX_SCAN_DEPTH = 4
# Poller interval (seconds)
_POLL_INTERVAL_SEC = 300


class GitCollector:
    """
    Watches git.tsv written by the global git hooks.

    Also starts a poller as a fallback for repos that already had
    core.hooksPath set before dev-recall was installed.
    """

    def __init__(
        self,
        git_tsv: Path,
        event_callback: Callable[[Event], None],
        get_kv: Callable[[str], Optional[str]],
        set_kv: Callable[[str, str], None],
        repo_ignore_patterns: Optional[list[str]] = None,
    ) -> None:
        self._path = git_tsv
        self._callback = event_callback
        self._get_kv = get_kv
        self._set_kv = set_kv
        self._ignore_patterns = repo_ignore_patterns or []

        stored_offset = get_kv(_OFFSET_KEY)
        self._offset: int = int(stored_offset) if stored_offset else 0

        self._observer: Optional[Observer] = None
        self._poller_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def start(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.touch(exist_ok=True)

        # Catch up on any lines written while daemon was down
        self._read_new_lines()

        handler = _GitTSVHandler(self._path, self._on_file_change)
        self._observer = Observer()
        self._observer.schedule(handler, str(self._path.parent), recursive=False)
        self._observer.start()

        self._poller_thread = threading.Thread(
            target=self._poller_loop,
            daemon=True,
            name="dev-recall-git-poller",
        )
        self._poller_thread.start()
        logger.info("GitCollector watching %s + poller started", self._path)

    def stop(self) -> None:
        self._stop_event.set()
        if self._observer:
            self._observer.stop()
            self._observer.join(timeout=5)
        logger.info("GitCollector stopped")

    # ------------------------------------------------------------------
    # File watcher
    # ------------------------------------------------------------------

    def _on_file_change(self) -> None:
        with self._lock:
            self._read_new_lines()

    def _read_new_lines(self) -> None:
        try:
            file_size = self._path.stat().st_size
        except OSError:
            return

        if file_size <= self._offset:
            if file_size < self._offset:
                logger.warning("git.tsv shrank; resetting offset")
                self._offset = 0
            return

        try:
            with self._path.open("rb") as fh:
                fh.seek(self._offset)
                new_bytes = fh.read()
                new_offset = fh.tell()
        except OSError as exc:
            logger.error("Cannot read git.tsv: %s", exc)
            return

        for raw_line in new_bytes.decode("utf-8", errors="replace").splitlines():
            event = self._parse_line(raw_line.rstrip("\r\n"))
            if event:
                try:
                    self._callback(event)
                except Exception:
                    logger.exception("Error in git event callback")

        self._offset = new_offset
        self._set_kv(_OFFSET_KEY, str(new_offset))

    def _parse_line(self, line: str) -> Optional[Event]:
        if not line.strip():
            return None
        parts = line.split("\t")
        if len(parts) < 3:
            return None

        ts_str = parts[0]
        kind = parts[1]

        try:
            dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            date_str = dt.strftime("%Y-%m-%d")
        except ValueError:
            dt = datetime.now(timezone.utc)
            date_str = dt.strftime("%Y-%m-%d")
            ts_str = dt.strftime("%Y-%m-%dT%H:%M:%SZ")

        if kind == "commit" and len(parts) >= _COMMIT_COLS:
            return self._parse_commit(ts_str, date_str, parts)
        elif kind == "branch" and len(parts) >= _BRANCH_COLS:
            return self._parse_branch(ts_str, date_str, parts)
        elif kind == "push" and len(parts) >= _PUSH_COLS:
            return self._parse_push(ts_str, date_str, parts)
        elif kind == "merge" and len(parts) >= _MERGE_COLS:
            return self._parse_merge(ts_str, date_str, parts)

        return None

    def _parse_commit(self, ts: str, date: str, parts: list[str]) -> Optional[Event]:
        repo_path = parts[2]
        commit_hash = parts[3]
        branch = parts[4]
        message = parts[5]
        files_raw = parts[6] if len(parts) > 6 else ""
        author = parts[7] if len(parts) > 7 else ""

        if self._is_repo_ignored(repo_path):
            return None

        repo_name = os.path.basename(repo_path.rstrip("/"))
        files = [f for f in files_raw.split("|") if f]

        raw = {
            "hash": commit_hash,
            "branch": branch,
            "message": message,
            "files": files,
            "author": author,
            "repo_path": repo_path,
            "repo_name": repo_name,
        }
        content = build_content(EventType.GIT_COMMIT, raw)

        event = Event(
            timestamp=ts,
            date=date,
            event_type=EventType.GIT_COMMIT,
            source=Source.GIT_HOOK,
            content=content,
            raw_data=raw,
            repo_path=repo_path,
            repo_name=repo_name,
        )
        return event

    def _parse_branch(self, ts: str, date: str, parts: list[str]) -> Optional[Event]:
        repo_path = parts[2]
        old_branch = parts[3]
        new_branch = parts[4]

        if self._is_repo_ignored(repo_path):
            return None

        repo_name = os.path.basename(repo_path.rstrip("/"))
        raw = {
            "old_branch": old_branch,
            "new_branch": new_branch,
            "repo_path": repo_path,
            "repo_name": repo_name,
        }
        content = build_content(EventType.GIT_BRANCH, raw)

        return Event(
            timestamp=ts,
            date=date,
            event_type=EventType.GIT_BRANCH,
            source=Source.GIT_HOOK,
            content=content,
            raw_data=raw,
            repo_path=repo_path,
            repo_name=repo_name,
        )

    def _parse_push(self, ts: str, date: str, parts: list[str]) -> Optional[Event]:
        repo_path = parts[2]
        remote = parts[3]
        branch = parts[4]
        try:
            commit_count = int(parts[5])
        except (ValueError, IndexError):
            commit_count = 0

        if self._is_repo_ignored(repo_path):
            return None

        repo_name = os.path.basename(repo_path.rstrip("/"))
        raw = {
            "remote": remote,
            "branch": branch,
            "commit_count": commit_count,
            "repo_path": repo_path,
            "repo_name": repo_name,
        }
        content = build_content(EventType.GIT_PUSH, raw)
        return Event(
            timestamp=ts,
            date=date,
            event_type=EventType.GIT_PUSH,
            source=Source.GIT_HOOK,
            content=content,
            raw_data=raw,
            repo_path=repo_path,
            repo_name=repo_name,
        )

    def _parse_merge(self, ts: str, date: str, parts: list[str]) -> Optional[Event]:
        repo_path = parts[2]
        branch = parts[3]
        merged_branch = parts[4]
        is_squash = parts[5].strip() == "1" if len(parts) > 5 else False

        if self._is_repo_ignored(repo_path):
            return None

        repo_name = os.path.basename(repo_path.rstrip("/"))
        raw = {
            "branch": branch,
            "merged_branch": merged_branch,
            "is_squash": is_squash,
            "repo_path": repo_path,
            "repo_name": repo_name,
        }
        content = build_content(EventType.GIT_MERGE, raw)
        return Event(
            timestamp=ts,
            date=date,
            event_type=EventType.GIT_MERGE,
            source=Source.GIT_HOOK,
            content=content,
            raw_data=raw,
            repo_path=repo_path,
            repo_name=repo_name,
        )

    def _is_repo_ignored(self, path: str) -> bool:
        for pattern in self._ignore_patterns:
            if fnmatch.fnmatch(path, pattern):
                return True
        return False

    # ------------------------------------------------------------------
    # Git poller fallback
    # ------------------------------------------------------------------

    def _poller_loop(self) -> None:
        """Runs every POLL_INTERVAL_SEC to catch commits missed by hooks."""
        # Wait a bit before first poll so daemon start-up completes
        self._stop_event.wait(30)

        while not self._stop_event.is_set():
            try:
                self._poll_git_repos()
            except Exception:
                logger.exception("Git poller error")
            self._stop_event.wait(_POLL_INTERVAL_SEC)

    def _poll_git_repos(self) -> None:
        last_run_str = self._get_kv(_LAST_POLL_KEY)
        if last_run_str:
            since = last_run_str
        else:
            # First run: only look back 24 hours
            from datetime import timedelta

            dt = datetime.now(timezone.utc) - timedelta(hours=24)
            since = dt.strftime("%Y-%m-%dT%H:%M:%SZ")

        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        git_repos = _find_git_repos(Path.home(), _MAX_SCAN_DEPTH)
        for repo_path in git_repos:
            if self._is_repo_ignored(str(repo_path)):
                continue
            self._poll_repo(repo_path, since)

        self._set_kv(_LAST_POLL_KEY, now_str)

    def _poll_repo(self, repo_path: Path, since: str) -> None:
        """Fetch commits from *repo_path* since *since* (ISO timestamp)."""
        try:
            result = subprocess.run(
                [
                    "git",
                    "-C",
                    str(repo_path),
                    "log",
                    f"--since={since}",
                    "--format=%H\x1f%s\x1f%D\x1f%an\x1f%aI",
                    "--name-only",
                    "--diff-filter=ACDMRT",
                ],
                capture_output=True,
                text=True,
                timeout=15,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return

        if result.returncode != 0:
            return

        repo_name = repo_path.name
        # Parse the custom format
        # Each commit block: header line + blank + file lines + blank
        commit_hash = branch = message = author = ts_str = ""
        files: list[str] = []

        for line in result.stdout.splitlines():
            if "\x1f" in line:
                # Header line
                if commit_hash and message:
                    self._emit_polled_commit(
                        repo_path, repo_name, commit_hash, branch,
                        message, author, files, ts_str,
                    )
                parts = line.split("\x1f")
                commit_hash = parts[0]
                message = parts[1] if len(parts) > 1 else ""
                refs = parts[2] if len(parts) > 2 else ""
                author = parts[3] if len(parts) > 3 else ""
                ts_str = parts[4] if len(parts) > 4 else ""
                # Extract branch from refs (e.g. "HEAD -> main, origin/main")
                branch = _extract_branch_from_refs(refs)
                files = []
            elif line.strip():
                files.append(line.strip())

        # Emit last commit
        if commit_hash and message:
            self._emit_polled_commit(
                repo_path, repo_name, commit_hash, branch,
                message, author, files, ts_str,
            )

    def _emit_polled_commit(
        self,
        repo_path: Path,
        repo_name: str,
        commit_hash: str,
        branch: str,
        message: str,
        author: str,
        files: list[str],
        ts_str: str,
    ) -> None:
        # Normalize timestamp
        try:
            dt = datetime.fromisoformat(ts_str)
            ts_out = dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            date_str = dt.astimezone(timezone.utc).strftime("%Y-%m-%d")
        except ValueError:
            now = datetime.now(timezone.utc)
            ts_out = now.strftime("%Y-%m-%dT%H:%M:%SZ")
            date_str = now.strftime("%Y-%m-%d")

        raw = {
            "hash": commit_hash,
            "branch": branch,
            "message": message,
            "files": files,
            "author": author,
            "repo_path": str(repo_path),
            "repo_name": repo_name,
        }
        content = build_content(EventType.GIT_COMMIT, raw)
        event = Event(
            timestamp=ts_out,
            date=date_str,
            event_type=EventType.GIT_COMMIT,
            source=Source.GIT_POLLER,
            content=content,
            raw_data=raw,
            repo_path=str(repo_path),
            repo_name=repo_name,
        )
        try:
            self._callback(event)
        except Exception:
            logger.exception("Error emitting polled commit")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _find_git_repos(root: Path, max_depth: int) -> list[Path]:
    """Walk *root* up to *max_depth* levels and return directories containing .git."""
    repos: list[Path] = []
    _walk(root, max_depth, repos)
    return repos


def _walk(path: Path, depth: int, result: list[Path]) -> None:
    if depth < 0:
        return
    try:
        for child in path.iterdir():
            if not child.is_dir():
                continue
            name = child.name
            if name.startswith(".") and name != ".git":
                continue  # skip hidden dirs except .git
            if name == ".git":
                result.append(path)
                return  # don't recurse into .git
            _walk(child, depth - 1, result)
    except (PermissionError, OSError):
        pass


class _GitTSVHandler(FileSystemEventHandler):
    def __init__(self, path: Path, callback: Callable[[], None]) -> None:
        self._path = str(path)
        self._callback = callback

    def on_modified(self, event: FileModifiedEvent) -> None:
        if not event.is_directory and event.src_path == self._path:
            self._callback()


def _extract_branch_from_refs(refs: str) -> str:
    """Extract the current branch name from the git log %D decorator string."""
    for part in refs.split(","):
        part = part.strip()
        if part.startswith("HEAD -> "):
            return part[len("HEAD -> "):]
    # Fallback: use the first ref that's not 'HEAD' or 'origin/...'
    for part in refs.split(","):
        part = part.strip()
        if part and not part.startswith("origin/") and part != "HEAD":
            return part
    return refs.strip() or "unknown"


# ---------------------------------------------------------------------------
# Hook installer
# ---------------------------------------------------------------------------


def install_global_hooks(hooks_src_dir: Path, git_hooks_dest: Path) -> None:
    """
    Copy git hooks to *git_hooks_dest* and configure git to use them globally.

    Warns (but does not fail) if core.hooksPath is already set to something
    else — the user can review and decide.
    """
    import shutil

    git_hooks_dest.mkdir(parents=True, exist_ok=True)
    for hook_name in ("post-commit", "post-checkout"):
        src = hooks_src_dir / hook_name
        dst = git_hooks_dest / hook_name
        if src.exists():
            shutil.copy2(str(src), str(dst))
            os.chmod(str(dst), 0o755)

    # Check existing core.hooksPath
    result = subprocess.run(
        ["git", "config", "--global", "core.hooksPath"],
        capture_output=True,
        text=True,
    )
    existing = result.stdout.strip()
    desired = str(git_hooks_dest)

    if existing and existing != desired:
        logger.warning(
            "core.hooksPath is already set to %r. "
            "Overriding to %r — existing hooks may stop working. "
            "Consider merging them manually.",
            existing,
            desired,
        )

    subprocess.run(
        ["git", "config", "--global", "core.hooksPath", desired],
        check=True,
    )
