"""Event enricher — resolves repo paths, builds content text, applies privacy filters."""

from __future__ import annotations

import fnmatch
import logging
import os
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Callable, Optional

from recall.models import Event, EventType, build_content

logger = logging.getLogger(__name__)

# Extension → language name lookup (keep small, no heavy dep)
_EXT_TO_LANG: dict[str, str] = {
    ".py": "python", ".js": "javascript", ".ts": "typescript", ".tsx": "tsx",
    ".jsx": "jsx", ".java": "java", ".go": "go", ".rs": "rust", ".rb": "ruby",
    ".php": "php", ".cs": "c#", ".cpp": "c++", ".c": "c", ".h": "c",
    ".swift": "swift", ".kt": "kotlin", ".scala": "scala", ".sh": "bash",
    ".zsh": "zsh", ".bash": "bash", ".fish": "fish", ".html": "html",
    ".css": "css", ".scss": "scss", ".sass": "sass", ".vue": "vue",
    ".json": "json", ".yaml": "yaml", ".yml": "yaml", ".toml": "toml",
    ".xml": "xml", ".md": "markdown", ".rst": "rst", ".sql": "sql",
    ".tf": "terraform", ".proto": "protobuf", ".r": "r", ".m": "matlab",
    ".ipynb": "jupyter",
}


class Enricher:
    """
    Enriches raw events before they are written to the database.

    Responsibilities:
      1. Resolve cwd / file path → repo_path, repo_name
      2. Upsert the repo in the repos table
      3. Re-build the content text with resolved repo_name
      4. Apply privacy filters (drop sensitive events)
      5. Detect language for file_save events
    """

    def __init__(
        self,
        cmd_ignore_patterns: list[str],
        file_ignore_patterns: list[str],
        repo_ignore_patterns: list[str],
        upsert_repo: Callable[[str, str, Optional[str], Optional[str]], None],
    ) -> None:
        self._cmd_ignore = [p.lower() for p in cmd_ignore_patterns]
        self._file_ignore = [p.lower() for p in file_ignore_patterns]
        self._repo_ignore = [p.lower() for p in repo_ignore_patterns]
        self._upsert_repo = upsert_repo
        # Simple in-process cache for path → repo resolutions
        self._repo_cache: dict[str, Optional[str]] = {}

    def enrich(self, event: Event) -> Optional[Event]:
        """
        Enrich *event* in-place.

        Returns the event (possibly mutated) or None if it should be dropped
        due to a privacy filter.
        """
        # 1. Privacy check on commands
        if event.event_type == EventType.TERMINAL_CMD:
            cmd = event.raw_data.get("cmd", "")
            if self._is_cmd_sensitive(cmd):
                logger.debug("Dropping sensitive cmd")
                return None

        # 2. Privacy check on file paths
        if event.event_type == EventType.FILE_SAVE:
            file_path = event.raw_data.get("file_path", "")
            if self._is_file_sensitive(file_path):
                logger.debug("Dropping sensitive file_save: %s", file_path)
                return None

        # 3. Resolve repo from event data
        repo_path = self._resolve_repo(event)
        if repo_path:
            event.repo_path = repo_path
            event.repo_name = os.path.basename(repo_path.rstrip("/"))
            # Privacy check on repo path
            if self._is_repo_ignored(repo_path):
                logger.debug("Dropping event from ignored repo: %s", repo_path)
                return None
            # Upsert into repos table
            try:
                self._upsert_repo(
                    repo_path,
                    event.repo_name,
                    event.timestamp,
                    self._get_remote_url(repo_path),
                )
            except Exception:
                logger.exception("upsert_repo failed")

        # 4. Language detection for file_save
        if event.event_type == EventType.FILE_SAVE:
            filename = event.raw_data.get("filename", "")
            if not event.raw_data.get("language"):
                lang = _detect_language(filename)
                event.raw_data["language"] = lang

        # 5. Rebuild content text with enriched repo_name
        if event.repo_name:
            event.raw_data["repo_name"] = event.repo_name
        event.content = build_content(event.event_type, event.raw_data)

        return event

    # ------------------------------------------------------------------
    # Repo resolution
    # ------------------------------------------------------------------

    def _resolve_repo(self, event: Event) -> Optional[str]:
        """Return the canonical repo root path for the event, or None."""
        # Already resolved by collector (git hooks always set repo_path)
        if event.repo_path:
            return self._find_git_root(event.repo_path) or event.repo_path

        # Try cwd for terminal commands
        if event.event_type == EventType.TERMINAL_CMD:
            cwd = event.raw_data.get("cwd", "")
            if cwd:
                return self._find_git_root(cwd)

        # Try file_path for file saves
        if event.event_type == EventType.FILE_SAVE:
            fp = event.raw_data.get("file_path", "")
            if fp:
                return self._find_git_root(str(Path(fp).parent))

        # Try workspace for VS Code events
        if event.source.value == "vscode_ext":
            ws = event.raw_data.get("repo_path", "")
            if ws:
                return ws

        return None

    def _find_git_root(self, path: str) -> Optional[str]:
        """Walk up from *path* to find the nearest .git directory."""
        if path in self._repo_cache:
            return self._repo_cache[path]

        result = _find_git_root_cached(path)
        self._repo_cache[path] = result
        return result

    # ------------------------------------------------------------------
    # Privacy helpers
    # ------------------------------------------------------------------

    def _is_cmd_sensitive(self, cmd: str) -> bool:
        cmd_l = cmd.lower()
        return any(fnmatch.fnmatch(cmd_l, p) for p in self._cmd_ignore)

    def _is_file_sensitive(self, file_path: str) -> bool:
        name_l = os.path.basename(file_path).lower()
        return any(fnmatch.fnmatch(name_l, p) for p in self._file_ignore)

    def _is_repo_ignored(self, repo_path: str) -> bool:
        path_l = repo_path.lower()
        return any(fnmatch.fnmatch(path_l, p) for p in self._repo_ignore)

    # ------------------------------------------------------------------
    # Remote URL (best-effort)
    # ------------------------------------------------------------------

    @staticmethod
    def _get_remote_url(repo_path: str) -> Optional[str]:
        try:
            result = subprocess.run(
                ["git", "-C", repo_path, "remote", "get-url", "origin"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            pass
        return None


# ---------------------------------------------------------------------------
# Helpers (module-level for caching)
# ---------------------------------------------------------------------------


@lru_cache(maxsize=512)
def _find_git_root_cached(path: str) -> Optional[str]:
    """Walk up from *path* looking for a .git directory (cached)."""
    current = Path(path)
    # Limit walk depth to 20 levels
    for _ in range(20):
        if not current.exists():
            break
        if (current / ".git").exists():
            return str(current)
        parent = current.parent
        if parent == current:
            break
        current = parent
    return None


def _detect_language(filename: str) -> str:
    """Return a language name from the file extension, or 'unknown'."""
    _, ext = os.path.splitext(filename.lower())
    return _EXT_TO_LANG.get(ext, "unknown")
