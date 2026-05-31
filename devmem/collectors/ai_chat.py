"""AI chat log collector — watches Copilot, Claude Code, Aider, and Cursor logs."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from watchdog.events import FileModifiedEvent, FileSystemEventHandler
from watchdog.observers import Observer

from devmem.models import Event, EventType, Source, build_content

logger = logging.getLogger(__name__)

# Maximum characters to store from AI chat messages
_MAX_CHARS = 200

# KV key prefix for per-file byte offsets
_OFFSET_KEY_PREFIX = "ai_chat_offset:"

# KV key for set of processed message content hashes
_HASH_SET_KEY = "ai_chat_hashes"


class AIChatCollector:
    """
    Watches known AI tool log directories for new conversation messages.

    Supported sources:
      - GitHub Copilot Chat  (~/.config/Code/User/workspaceStorage/*/GitHub.copilot-chat/debug-logs/*.jsonl)
      - Claude Code          (~/.claude/projects/*/sessions/*.jsonl)
      - Aider                (.aider.chat.history.md in any git repo root)
      - Cursor               (~/.config/Cursor/User/workspaceStorage/*)
      - Gemini CLI           (~/.gemini/logs/*.jsonl)
      - Continue.dev         (~/.continue/sessions/*.json)
    """

    def __init__(
        self,
        event_callback: Callable[[Event], None],
        get_kv: Callable[[str], Optional[str]],
        set_kv: Callable[[str, str], None],
        ai_chat_max_chars: int = _MAX_CHARS,
    ) -> None:
        self._callback = event_callback
        self._get_kv = get_kv
        self._set_kv = set_kv
        self._max_chars = ai_chat_max_chars

        self._observer = Observer()
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        # file_path → byte offset
        self._offsets: dict[str, int] = {}

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def start(self) -> None:
        watch_dirs = self._collect_watch_dirs()
        for watch_dir, recursive in watch_dirs:
            if watch_dir.exists():
                handler = _AIChatHandler(self, watch_dir)
                self._observer.schedule(handler, str(watch_dir), recursive=recursive)
                logger.info("AIChatCollector watching %s (recursive=%s)", watch_dir, recursive)

        # Aider: watch each git repo root found at startup so live edits are captured
        for repo_root in _find_git_repos_shallow(Path.home(), depth=3):
            handler = _AIChatHandler(self, repo_root)
            try:
                self._observer.schedule(handler, str(repo_root), recursive=False)
            except Exception:
                pass

        # Scan existing files on startup
        self._scan_existing()

        self._observer.start()
        logger.info("AIChatCollector started")

    def stop(self) -> None:
        self._stop_event.set()
        self._observer.stop()
        self._observer.join(timeout=5)
        logger.info("AIChatCollector stopped")

    def handle_file_change(self, file_path: Path) -> None:
        """Called by watchdog handler when a watched file changes."""
        with self._lock:
            self._process_file(file_path)

    # ------------------------------------------------------------------
    # Watch directories
    # ------------------------------------------------------------------

    @staticmethod
    def _collect_watch_dirs() -> list[tuple[Path, bool]]:
        """Return (directory, recursive) pairs to watch."""
        home = Path.home()
        dirs = [
            # Copilot Chat
            (home / ".config" / "Code" / "User" / "workspaceStorage", True),
            # Claude Code
            (home / ".claude" / "projects", True),
            # Cursor
            (home / ".config" / "Cursor" / "User" / "workspaceStorage", True),
            # Gemini CLI
            (home / ".gemini" / "logs", False),
            # Continue.dev
            (home / ".continue" / "sessions", False),
        ]
        return [(d, recursive) for d, recursive in dirs]

    # ------------------------------------------------------------------
    # Scan on startup
    # ------------------------------------------------------------------

    def _scan_existing(self) -> None:
        """Process any log files that already exist (catch-up on daemon start)."""
        home = Path.home()
        jsonl_bases = [
            home / ".config" / "Code" / "User" / "workspaceStorage",
            home / ".claude" / "projects",
            home / ".config" / "Cursor" / "User" / "workspaceStorage",
        ]
        for base in jsonl_bases:
            if not base.exists():
                continue
            for jsonl in base.rglob("*.jsonl"):
                self._process_file(jsonl)

        # Gemini CLI logs
        gemini_dir = home / ".gemini" / "logs"
        if gemini_dir.exists():
            for f in gemini_dir.glob("*.jsonl"):
                self._process_file(f)

        # Continue.dev sessions
        continue_dir = home / ".continue" / "sessions"
        if continue_dir.exists():
            for f in continue_dir.glob("*.json"):
                self._process_file(f)

        # Aider: scan git repos for .aider.chat.history.md
        for repo_root in _find_git_repos_shallow(home, depth=3):
            aider_log = repo_root / ".aider.chat.history.md"
            if aider_log.exists():
                self._process_file(aider_log)

    # ------------------------------------------------------------------
    # File processing
    # ------------------------------------------------------------------

    def _process_file(self, path: Path) -> None:
        suffix = path.suffix.lower()
        name = path.name
        path_str = str(path)
        if suffix == ".jsonl":
            if "copilot-chat" in path_str or "GitHub.copilot-chat" in path_str:
                self._process_copilot_jsonl(path)
            elif ".claude" in path_str:
                self._process_claude_jsonl(path)
            elif "Cursor" in path_str:
                self._process_copilot_jsonl(path)  # same format
            elif ".gemini" in path_str:
                self._process_gemini_jsonl(path)
        elif suffix == ".json" and ".continue" in path_str:
            self._process_continue_session_json(path)
        elif name == ".aider.chat.history.md":
            self._process_aider_md(path)

    def _get_file_offset(self, path: Path) -> int:
        key = _OFFSET_KEY_PREFIX + str(path)
        stored = self._offsets.get(str(path))
        if stored is not None:
            return stored
        kv = self._get_kv(key)
        offset = int(kv) if kv else 0
        self._offsets[str(path)] = offset
        return offset

    def _set_file_offset(self, path: Path, offset: int) -> None:
        self._offsets[str(path)] = offset
        self._set_kv(_OFFSET_KEY_PREFIX + str(path), str(offset))

    def _read_new_content(self, path: Path) -> Optional[bytes]:
        """Read new bytes since last offset, returning None on error."""
        offset = self._get_file_offset(path)
        try:
            size = path.stat().st_size
        except OSError:
            return None
        if size <= offset:
            return None
        try:
            with path.open("rb") as fh:
                fh.seek(offset)
                content = fh.read()
                self._set_file_offset(path, fh.tell())
            return content
        except OSError:
            return None

    # ------------------------------------------------------------------
    # Copilot / Cursor JSONL parser
    # ------------------------------------------------------------------

    def _process_copilot_jsonl(self, path: Path) -> None:
        new_bytes = self._read_new_content(path)
        if not new_bytes:
            return

        # Derive workspace/repo from path
        repo_name = _repo_name_from_copilot_path(path)
        ai_source = "copilot" if "copilot" in str(path).lower() else "cursor"

        for raw_line in new_bytes.decode("utf-8", errors="replace").splitlines():
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                entry = json.loads(raw_line)
            except json.JSONDecodeError:
                continue

            events = self._extract_copilot_messages(entry, repo_name, ai_source)
            for event in events:
                if not self._is_duplicate(event):
                    try:
                        self._callback(event)
                    except Exception:
                        logger.exception("Error in ai_chat event callback")

    def _extract_copilot_messages(
        self, entry: dict, repo_name: str, ai_source: str
    ) -> list[Event]:
        results: list[Event]= []
        # Copilot debug logs have various shapes — try common structures
        messages: list[dict] = []

        if "messages" in entry and isinstance(entry["messages"], list):
            messages = entry["messages"]
        elif "request" in entry and isinstance(entry.get("request"), dict):
            req = entry["request"]
            if "messages" in req:
                messages = req["messages"]
        elif "role" in entry and "content" in entry:
            messages = [entry]

        ts = _extract_ts(entry)
        for msg in messages:
            event = self._build_ai_event(msg, ts, repo_name, ai_source, Source.AI_CHAT_PARSER)
            if event:
                results.append(event)
        return results

    # ------------------------------------------------------------------
    # Claude Code JSONL parser
    # ------------------------------------------------------------------

    def _process_claude_jsonl(self, path: Path) -> None:
        new_bytes = self._read_new_content(path)
        if not new_bytes:
            return

        repo_name = _repo_name_from_claude_path(path)

        for raw_line in new_bytes.decode("utf-8", errors="replace").splitlines():
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                entry = json.loads(raw_line)
            except json.JSONDecodeError:
                continue

            ts = _extract_ts(entry)
            # Claude Code entries typically have "role" and "content" at the top level
            msg_list: list[dict] = []
            if "role" in entry:
                msg_list = [entry]
            elif "messages" in entry and isinstance(entry["messages"], list):
                msg_list = entry["messages"]

            for msg in msg_list:
                event = self._build_ai_event(msg, ts, repo_name, "claude", Source.AI_CHAT_PARSER)
                if event and not self._is_duplicate(event):
                    try:
                        self._callback(event)
                    except Exception:
                        logger.exception("Error in claude event callback")

    # ------------------------------------------------------------------
    # Gemini CLI JSONL parser
    # ------------------------------------------------------------------

    def _process_gemini_jsonl(self, path: Path) -> None:
        new_bytes = self._read_new_content(path)
        if not new_bytes:
            return

        for raw_line in new_bytes.decode("utf-8", errors="replace").splitlines():
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                entry = json.loads(raw_line)
            except json.JSONDecodeError:
                continue

            # Gemini CLI stores {role, parts: [{text: ...}]} or {role, content: ...}
            role = entry.get("role", "")
            if role not in ("user", "model", "assistant"):
                # Might be a wrapper — try extracting messages list
                messages = entry.get("messages") or entry.get("contents", [])
                ts = _extract_ts(entry)
                for msg in messages if isinstance(messages, list) else []:
                    normalized_role = "assistant" if msg.get("role") in ("model",) else msg.get("role", "")
                    event = self._build_ai_event(
                        {"role": normalized_role, "content": _extract_gemini_text(msg)},
                        ts, "", "gemini", Source.AI_CHAT_PARSER,
                    )
                    if event and not self._is_duplicate(event):
                        try:
                            self._callback(event)
                        except Exception:
                            logger.exception("Error in gemini event callback")
                continue

            # Normalize "model" → "assistant"
            normalized_role = "assistant" if role == "model" else role
            text = _extract_gemini_text(entry)
            ts = _extract_ts(entry)
            event = self._build_ai_event(
                {"role": normalized_role, "content": text},
                ts, "", "gemini", Source.AI_CHAT_PARSER,
            )
            if event and not self._is_duplicate(event):
                try:
                    self._callback(event)
                except Exception:
                    logger.exception("Error in gemini event callback")

    # ------------------------------------------------------------------
    # Continue.dev session JSON parser
    # ------------------------------------------------------------------

    def _process_continue_session_json(self, path: Path) -> None:
        """Parse a Continue.dev session JSON file.

        Format: list of {"message": {"role": ..., "content": ...}, ...}
        or: {"sessionId": ..., "history": [{"message": {...}}, ...]}
        """
        try:
            raw = path.read_bytes()
        except OSError:
            return

        # Only re-process if file content changed (use size as proxy)
        offset = self._get_file_offset(path)
        current_size = len(raw)
        if current_size <= offset:
            return
        self._set_file_offset(path, current_size)

        try:
            data = json.loads(raw.decode("utf-8", errors="replace"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return

        repo_name = path.stem  # session ID as a proxy name
        now_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        # Normalise to a flat list of messages
        messages: list[dict] = []
        if isinstance(data, list):
            # [{"message": {"role": ..., "content": ...}}, ...]
            for item in data:
                if isinstance(item, dict):
                    msg = item.get("message", item)
                    messages.append(msg)
        elif isinstance(data, dict):
            history = data.get("history", data.get("messages", []))
            for item in history if isinstance(history, list) else []:
                if isinstance(item, dict):
                    msg = item.get("message", item)
                    messages.append(msg)

        for msg in messages:
            ts = _extract_ts(msg) if isinstance(msg, dict) else now_ts
            event = self._build_ai_event(msg, ts, repo_name, "continue", Source.AI_CHAT_PARSER)
            if event and not self._is_duplicate(event):
                try:
                    self._callback(event)
                except Exception:
                    logger.exception("Error in continue.dev event callback")

    # ------------------------------------------------------------------
    # Aider markdown parser
    # ------------------------------------------------------------------

    def _process_aider_md(self, path: Path) -> None:
        new_bytes = self._read_new_content(path)
        if not new_bytes:
            return

        repo_name = path.parent.name
        text = new_bytes.decode("utf-8", errors="replace")

        # Aider uses "> " prefix for user messages and no prefix for assistant
        # Format: #### timestamp\n> user message\n\nassistant message\n\n
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        now_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        for chunk in re.split(r"^####\s+", text, flags=re.MULTILINE):
            if not chunk.strip():
                continue
            lines = chunk.splitlines()
            ts_str = now_str
            date_str = now_date
            # Try to parse timestamp from first line
            if lines:
                try:
                    dt = datetime.fromisoformat(lines[0].strip().replace("Z", "+00:00"))
                    ts_str = dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                    date_str = dt.astimezone(timezone.utc).strftime("%Y-%m-%d")
                    lines = lines[1:]
                except ValueError:
                    pass

            body = "\n".join(lines)
            # User lines start with "> "
            user_parts = []
            assistant_parts = []
            for line in body.splitlines():
                if line.startswith("> "):
                    user_parts.append(line[2:])
                else:
                    assistant_parts.append(line)

            for role, parts in [("user", user_parts), ("assistant", assistant_parts)]:
                if not parts:
                    continue
                preview = " ".join(parts).strip()[: self._max_chars]
                if not preview:
                    continue
                raw = {
                    "role": role,
                    "message_preview": preview,
                    "repo_name": repo_name,
                    "ai_source": "aider",
                }
                event = Event(
                    timestamp=ts_str,
                    date=date_str,
                    event_type=EventType.AI_CHAT,
                    source=Source.AI_CHAT_PARSER,
                    content=build_content(EventType.AI_CHAT, raw),
                    raw_data=raw,
                    repo_name=repo_name,
                    repo_path=str(path.parent),
                )
                if not self._is_duplicate(event):
                    try:
                        self._callback(event)
                    except Exception:
                        logger.exception("Error in aider event callback")

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    def _build_ai_event(
        self,
        msg: dict,
        ts: str,
        repo_name: str,
        ai_source: str,
        source: Source,
    ) -> Optional[Event]:
        role = msg.get("role", "")
        if role not in ("user", "assistant"):
            return None

        content_raw = msg.get("content", "")
        if isinstance(content_raw, list):
            # Content can be an array of content blocks
            parts = [
                block.get("text", "") if isinstance(block, dict) else str(block)
                for block in content_raw
            ]
            content_raw = " ".join(parts)

        preview = str(content_raw).strip()[: self._max_chars]
        if not preview:
            return None

        # Derive date from timestamp
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            date_str = dt.astimezone(timezone.utc).strftime("%Y-%m-%d")
        except ValueError:
            dt = datetime.now(timezone.utc)
            ts = dt.strftime("%Y-%m-%dT%H:%M:%SZ")
            date_str = dt.strftime("%Y-%m-%d")

        raw = {
            "role": role,
            "message_preview": preview,
            "repo_name": repo_name,
            "ai_source": ai_source,
        }
        return Event(
            timestamp=ts,
            date=date_str,
            event_type=EventType.AI_CHAT,
            source=source,
            content=build_content(EventType.AI_CHAT, raw),
            raw_data=raw,
            repo_name=repo_name if repo_name else None,
        )

    def _is_duplicate(self, event: Event) -> bool:
        """Return True if this event's content hash has been seen before.

        The hash includes both content and timestamp so that the same message
        sent in two different sessions is not incorrectly deduplicated.
        """
        h = hashlib.sha256(f"{event.timestamp}\x00{event.content}".encode()).hexdigest()[:16]
        seen_json = self._get_kv(_HASH_SET_KEY) or "[]"
        try:
            seen: list[str] = json.loads(seen_json)
        except json.JSONDecodeError:
            seen = []
        if h in seen:
            return True
        # Keep last 10 000 hashes to bound storage
        seen.append(h)
        if len(seen) > 10_000:
            seen = seen[-10_000:]
        self._set_kv(_HASH_SET_KEY, json.dumps(seen))
        return False


# ---------------------------------------------------------------------------
# Watchdog handler
# ---------------------------------------------------------------------------


class _AIChatHandler(FileSystemEventHandler):
    def __init__(self, collector: AIChatCollector, base_dir: Path) -> None:
        self._collector = collector
        self._base = base_dir

    def on_modified(self, event: FileModifiedEvent) -> None:
        if event.is_directory:
            return
        path = Path(str(event.src_path))
        if path.suffix in (".jsonl", ".json") or path.name == ".aider.chat.history.md":
            self._collector.handle_file_change(path)

    def on_created(self, event) -> None:
        self.on_modified(event)


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def _repo_name_from_copilot_path(path: Path) -> str:
    """
    Copilot logs live inside workspaceStorage/<hash>/GitHub.copilot-chat/debug-logs/
    The workspace name is encoded in the hash or the parent workspaceStorage metadata.
    Best we can do without reading VS Code internals: use the hash folder name.
    """
    parts = path.parts
    for i, part in enumerate(parts):
        if part == "workspaceStorage" and i + 1 < len(parts):
            return parts[i + 1][:8]  # short hash
    return ""


def _repo_name_from_claude_path(path: Path) -> str:
    """Claude projects: ~/.claude/projects/<project_name>/sessions/*.jsonl"""
    parts = path.parts
    for i, part in enumerate(parts):
        if part == "projects" and i + 1 < len(parts):
            return parts[i + 1]
    return ""


def _extract_ts(entry: dict) -> str:
    """Extract a timestamp from a log entry dict, falling back to now."""
    for key in ("timestamp", "ts", "time", "created_at", "date"):
        if key in entry:
            val = str(entry[key])
            try:
                dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
                return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            except ValueError:
                pass
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _find_git_repos_shallow(root: Path, depth: int) -> list[Path]:
    repos: list[Path] = []
    try:
        for child in root.iterdir():
            if not child.is_dir() or child.name.startswith("."):
                continue
            if (child / ".git").exists():
                repos.append(child)
            elif depth > 1:
                repos.extend(_find_git_repos_shallow(child, depth - 1))
    except (PermissionError, OSError):
        pass
    return repos


def _extract_gemini_text(entry: dict) -> str:
    """Extract plain text from a Gemini CLI message entry.

    Gemini uses `parts: [{text: ...}]` or `content: ...`.
    """
    # parts: [{text: ...}, ...]
    parts = entry.get("parts") or entry.get("content", [])
    if isinstance(parts, list):
        texts = []
        for part in parts:
            if isinstance(part, dict):
                texts.append(str(part.get("text", "")))
            elif isinstance(part, str):
                texts.append(part)
        return " ".join(texts)
    if isinstance(parts, str):
        return parts
    return str(entry.get("text", ""))
