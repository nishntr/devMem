"""DevMem data models — Event dataclass and enums."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class EventType(str, Enum):
    TERMINAL_CMD = "terminal_cmd"
    GIT_COMMIT = "git_commit"
    GIT_BRANCH = "git_branch_switch"
    GIT_PUSH = "git_push"
    GIT_MERGE = "git_merge"
    FILE_SAVE = "file_save"
    FILE_CREATE = "file_create"
    FILE_DELETE = "file_delete"
    FILE_RENAME = "file_rename"
    REPO_OPEN = "repo_open"
    REPO_CLOSE = "repo_close"
    AI_CHAT = "ai_chat"
    DEBUG_SESSION = "debug_session"
    TEST_RUN = "test_run"


class Source(str, Enum):
    SHELL_HOOK = "shell_hook"
    GIT_HOOK = "git_hook"
    GIT_POLLER = "git_poller"
    VSCODE_EXT = "vscode_ext"
    AI_CHAT_PARSER = "ai_chat_parser"


@dataclass
class Event:
    """A single captured developer activity event."""

    timestamp: str  # ISO 8601 UTC, e.g. "2026-05-21T14:30:00Z"
    date: str  # YYYY-MM-DD derived from timestamp
    event_type: EventType
    source: Source
    content: str  # Human-readable text used for embedding & display
    raw_data: dict[str, Any]  # Original parsed data
    metadata: dict[str, Any] = field(default_factory=dict)
    repo_path: Optional[str] = None  # Canonical absolute path
    repo_name: Optional[str] = None  # Basename
    session_id: Optional[str] = None  # UUID
    embedding_id: Optional[int] = None
    id: Optional[int] = None  # Set after DB insert

    # ------------------------------------------------------------------
    # Serialization helpers
    # ------------------------------------------------------------------

    def to_db_row(self) -> dict[str, Any]:
        """Return a dict suitable for inserting into the SQLite events table."""
        return {
            "timestamp": self.timestamp,
            "date": self.date,
            "event_type": self.event_type.value,
            "source": self.source.value,
            "content": self.content,
            "raw_data": json.dumps(self.raw_data),
            "metadata": json.dumps(self.metadata),
            "repo_path": self.repo_path,
            "repo_name": self.repo_name,
            "session_id": self.session_id,
            "embedding_id": self.embedding_id,
        }

    @classmethod
    def from_db_row(cls, row: dict[str, Any]) -> "Event":
        """Reconstruct an Event from a SQLite row dict."""
        return cls(
            id=row["id"],
            timestamp=row["timestamp"],
            date=row["date"],
            event_type=EventType(row["event_type"]),
            source=Source(row["source"]),
            content=row["content"],
            raw_data=json.loads(row["raw_data"]) if row["raw_data"] else {},
            metadata=json.loads(row["metadata"]) if row["metadata"] else {},
            repo_path=row.get("repo_path"),
            repo_name=row.get("repo_name"),
            session_id=row.get("session_id"),
            embedding_id=row.get("embedding_id"),
        )

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable dict."""
        return {
            "id": self.id,
            "timestamp": self.timestamp,
            "date": self.date,
            "event_type": self.event_type.value,
            "source": self.source.value,
            "content": self.content,
            "raw_data": self.raw_data,
            "metadata": self.metadata,
            "repo_path": self.repo_path,
            "repo_name": self.repo_name,
            "session_id": self.session_id,
            "embedding_id": self.embedding_id,
        }


# ---------------------------------------------------------------------------
# Content text builders
# ---------------------------------------------------------------------------


def build_content(event_type: EventType, data: dict[str, Any]) -> str:
    """Build the human-readable content string for an event from its raw data.

    This is the text used for both display and embedding.
    """
    match event_type:
        case EventType.TERMINAL_CMD:
            cmd = data.get("cmd", "")
            repo = data.get("repo_name") or _basename(data.get("cwd", ""))
            exit_code = data.get("exit_code", 0)
            duration = data.get("duration_ms", 0)
            return f"[terminal] {cmd} in {repo} (exit {exit_code}, {duration}ms)"

        case EventType.GIT_COMMIT:
            message = data.get("message", "")
            branch = data.get("branch", "")
            repo = data.get("repo_name", "")
            files = data.get("files", [])
            files_str = ", ".join(files[:5]) if files else ""
            if len(files) > 5:
                files_str += f" (+{len(files) - 5} more)"
            return f"[git] commit '{message}' to {branch} in {repo}. Changed: {files_str}"

        case EventType.GIT_BRANCH:
            new_branch = data.get("new_branch", "")
            old_branch = data.get("old_branch", "")
            repo = data.get("repo_name", "")
            return f"[git] switched to branch '{new_branch}' in {repo} (from '{old_branch}')"

        case EventType.GIT_PUSH:
            repo = data.get("repo_name", "")
            branch = data.get("branch", "")
            remote = data.get("remote", "origin")
            count = data.get("commit_count", 0)
            return f"[git] pushed {count} commit(s) on {branch} to {remote} in {repo}"

        case EventType.GIT_MERGE:
            repo = data.get("repo_name", "")
            branch = data.get("branch", "")
            merged = data.get("merged_branch", "")
            squash = data.get("is_squash", False)
            merge_type = "squash-merged" if squash else "merged"
            merged_str = f" '{merged}'" if merged else ""
            return f"[git] {merge_type}{merged_str} into {branch} in {repo}"

        case EventType.FILE_SAVE:
            filename = data.get("filename", "")
            language = data.get("language", "")
            repo = data.get("repo_name", "")
            return f"[edit] saved {filename} ({language}) in {repo}"

        case EventType.FILE_CREATE:
            filename = data.get("filename", "")
            repo = data.get("repo_name", "")
            return f"[edit] created {filename} in {repo}"

        case EventType.FILE_DELETE:
            filename = data.get("filename", "")
            repo = data.get("repo_name", "")
            return f"[edit] deleted {filename} in {repo}"

        case EventType.FILE_RENAME:
            old = data.get("old_filename", "")
            new = data.get("new_filename", "")
            repo = data.get("repo_name", "")
            return f"[edit] renamed {old} -> {new} in {repo}"

        case EventType.REPO_OPEN:
            repo = data.get("repo_name", "")
            path = data.get("repo_path", "")
            return f"[workspace] opened {repo} ({path})"

        case EventType.REPO_CLOSE:
            repo = data.get("repo_name", "")
            duration = data.get("duration_minutes", 0)
            return f"[workspace] closed {repo} after {duration} minutes"

        case EventType.AI_CHAT:
            source = data.get("ai_source", "ai")
            role = data.get("role", "user")
            preview = data.get("message_preview", "")[:150]
            repo = data.get("repo_name", "")
            suffix = f" in {repo}" if repo else ""
            return f"[{source} chat] {role}: {preview}{suffix}"

        case EventType.DEBUG_SESSION:
            name = data.get("name", "")
            repo = data.get("repo_name", "")
            action = data.get("action", "started")
            debug_type = data.get("debug_type", "")
            type_str = f" ({debug_type})" if debug_type else ""
            return f"[debug] {action} session '{name}'{type_str} in {repo}"

        case EventType.TEST_RUN:
            name = data.get("name", "")
            repo = data.get("repo_name", "")
            action = data.get("action", "started")
            exit_code = data.get("exit_code")
            if action == "finished" and exit_code is not None:
                status = "passed" if exit_code == 0 else "failed"
                return f"[test] '{name}' {status} in {repo}"
            return f"[test] {action} '{name}' in {repo}"

        case _:
            return str(data)


def _basename(path: str) -> str:
    """Return the last component of a path string."""
    import os

    return os.path.basename(path.rstrip("/")) or path
