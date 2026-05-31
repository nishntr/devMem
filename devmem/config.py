"""DevMem configuration — load, save, and default settings."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from platformdirs import user_config_dir, user_data_dir

_APP_NAME = "devmem"

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULT_CONFIG: dict[str, Any] = {
    "data_dir": str(Path(user_data_dir(_APP_NAME))),
    "config_dir": str(Path(user_config_dir(_APP_NAME))),
    "daemon_port": 27182,
    "embedding_model": "all-MiniLM-L6-v2",
    "embedding_dim": 384,
    "llm_model": "anthropic/claude-sonnet-4",
    "retention_days": 90,
    "capture": {
        "terminal": True,
        "git": True,
        "vscode": True,
        "ai_chat": True,
    },
    "privacy": {
        "cmd_ignore_patterns": [
            "*password*",
            "*passwd*",
            "*secret*",
            "*token*",
            "*apikey*",
            "*api_key*",
            "sudo *",
            "*credential*",
        ],
        "file_ignore_patterns": [
            "*.env",
            "*.pem",
            "*.key",
            "*.p12",
            ".env.*",
        ],
        "repo_ignore_patterns": [],
        "ai_chat_max_chars": 200,
    },
    "summary": {
        "auto_generate": True,
        "time": "23:30",
    },
    "search": {
        "default_top_k": 10,
        "rrf_k": 60,
        "session_idle_minutes": 30,
    },
}


# ---------------------------------------------------------------------------
# Config class
# ---------------------------------------------------------------------------


class Config:
    """Loaded and saveable configuration."""

    def __init__(self, data: dict[str, Any]) -> None:
        self._data: dict[str, Any] = data

    # ------------------------------------------------------------------
    # Top-level properties
    # ------------------------------------------------------------------

    @property
    def data_dir(self) -> Path:
        return Path(self._data["data_dir"]).expanduser()

    @property
    def config_dir(self) -> Path:
        return Path(self._data["config_dir"]).expanduser()

    @property
    def daemon_port(self) -> int:
        return int(self._data.get("daemon_port", 27182))

    @property
    def embedding_model(self) -> str:
        return str(self._data.get("embedding_model", "all-MiniLM-L6-v2"))

    @property
    def embedding_dim(self) -> int:
        return int(self._data.get("embedding_dim", 384))

    @property
    def llm_model(self) -> str:
        return str(self._data.get("llm_model", "anthropic/claude-sonnet-4"))

    @property
    def retention_days(self) -> int:
        return int(self._data.get("retention_days", 90))

    # Nested sections as plain dicts (read-only convenience)
    @property
    def capture(self) -> dict[str, Any]:
        return dict(self._data.get("capture", {}))

    @property
    def privacy(self) -> dict[str, Any]:
        return dict(self._data.get("privacy", {}))

    @property
    def summary(self) -> dict[str, Any]:
        return dict(self._data.get("summary", {}))

    @property
    def search(self) -> dict[str, Any]:
        return dict(self._data.get("search", {}))

    # ------------------------------------------------------------------
    # Derived paths
    # ------------------------------------------------------------------

    @property
    def db_path(self) -> Path:
        return self.data_dir / "events.db"

    @property
    def faiss_path(self) -> Path:
        return self.data_dir / "vectors.faiss"

    @property
    def shell_tsv_path(self) -> Path:
        return self.data_dir / "shell.tsv"

    @property
    def git_tsv_path(self) -> Path:
        return self.data_dir / "git.tsv"

    @property
    def pid_path(self) -> Path:
        return self.data_dir / "daemon.pid"

    @property
    def log_path(self) -> Path:
        return self.data_dir / "daemon.log"

    @property
    def hook_zsh_path(self) -> Path:
        return self.config_dir / "hook.zsh"

    @property
    def hook_bash_path(self) -> Path:
        return self.config_dir / "hook.bash"

    @property
    def hook_fish_path(self) -> Path:
        return self.config_dir / "hook.fish"

    @property
    def git_hooks_dir(self) -> Path:
        return self.config_dir / "git-hooks"

    # ------------------------------------------------------------------
    # Get / set by dot-path key (e.g. "privacy.retention_days")
    # ------------------------------------------------------------------

    def get(self, key: str) -> Any:
        parts = key.split(".")
        node: Any = self._data
        for part in parts:
            if not isinstance(node, dict) or part not in node:
                return None
            node = node[part]
        return node

    def set(self, key: str, value: Any) -> None:
        parts = key.split(".")
        node = self._data
        for part in parts[:-1]:
            if part not in node or not isinstance(node[part], dict):
                node[part] = {}
            node = node[part]
        # Attempt to coerce type to match existing value
        leaf = parts[-1]
        if leaf in node and node[leaf] is not None:
            existing = node[leaf]
            try:
                if isinstance(existing, bool):
                    value = value.lower() in ("true", "1", "yes") if isinstance(value, str) else bool(value)
                elif isinstance(existing, int):
                    value = int(value)
                elif isinstance(existing, float):
                    value = float(value)
            except (ValueError, AttributeError):
                pass
        node[leaf] = value

    def as_dict(self) -> dict[str, Any]:
        return dict(self._data)

    def __repr__(self) -> str:
        return f"Config(data_dir={self.data_dir}, config_dir={self.config_dir})"


# ---------------------------------------------------------------------------
# Load / save helpers
# ---------------------------------------------------------------------------


def _config_file_path() -> Path:
    """Return the path to the config JSON file, respecting DEVMEM_CONFIG env override."""
    env_path = os.environ.get("DEVMEM_CONFIG")
    if env_path:
        return Path(env_path).expanduser()
    return Path(user_config_dir(_APP_NAME)) / "config.json"


def load_config() -> Config:
    """Load config from disk, merging with defaults for missing keys."""
    path = _config_file_path()
    if not path.exists():
        return Config(_deep_merge({}, DEFAULT_CONFIG))

    try:
        with path.open("r", encoding="utf-8") as fh:
            user_data = json.load(fh)
    except (json.JSONDecodeError, OSError):
        user_data = {}

    merged = _deep_merge(user_data, DEFAULT_CONFIG)
    return Config(merged)


def save_config(config: Config) -> None:
    """Persist config to disk."""
    path = _config_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(config.as_dict(), fh, indent=2)
        fh.write("\n")


def _deep_merge(user: dict, defaults: dict) -> dict:
    """Return a new dict that is *defaults* overlaid with *user* values (deep)."""
    result = dict(defaults)
    for k, v in user.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(v, result[k])
        else:
            result[k] = v
    return result
