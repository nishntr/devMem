"""VS Code extension HTTP receiver — /event endpoint for the TypeScript extension."""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Callable, Optional

from recall.models import Event, EventType, Source, build_content

logger = logging.getLogger(__name__)

# Map VS Code extension event types to our internal EventType
_TYPE_MAP: dict[str, EventType] = {
    "workspace_open": EventType.REPO_OPEN,
    "workspace_close": EventType.REPO_CLOSE,
    "file_save": EventType.FILE_SAVE,
    "active_time": EventType.REPO_OPEN,  # treated as "still active" keep-alive
    "file_create": EventType.FILE_CREATE,
    "file_delete": EventType.FILE_DELETE,
    "file_rename": EventType.FILE_RENAME,
    "debug_session_start": EventType.DEBUG_SESSION,
    "debug_session_end": EventType.DEBUG_SESSION,
    "test_run_start": EventType.TEST_RUN,
    "test_run_finish": EventType.TEST_RUN,
}


def parse_vscode_event(
    payload: dict,
    event_callback: Callable[[Event], None],
) -> None:
    """
    Parse an incoming JSON payload from the VS Code extension and emit an Event.

    Payload fields (all optional except `type`):
      type        : str   — workspace_open | workspace_close | file_save | active_time
      ts          : str   — ISO 8601 timestamp (default: now)
      workspace   : str   — workspace folder path
      file        : str   — file path (for file_save)
      language    : str   — language id (for file_save)
      seconds_active : int — (for active_time)
    """
    event_type_str = str(payload.get("type", ""))
    event_type = _TYPE_MAP.get(event_type_str)
    if event_type is None:
        logger.debug("Ignoring unknown VS Code event type: %r", event_type_str)
        return

    ts_str = str(payload.get("ts", ""))
    try:
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        ts_out = dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        date_str = dt.astimezone(timezone.utc).strftime("%Y-%m-%d")
    except ValueError:
        dt = datetime.now(timezone.utc)
        ts_out = dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        date_str = dt.strftime("%Y-%m-%d")

    workspace = str(payload.get("workspace", ""))
    repo_path: Optional[str] = workspace if workspace else None
    repo_name: Optional[str] = os.path.basename(workspace.rstrip("/")) if workspace else None

    if event_type == EventType.FILE_SAVE:
        file_path = str(payload.get("file", ""))
        language = str(payload.get("language", ""))
        filename = os.path.basename(file_path)
        raw = {
            "filename": filename,
            "file_path": file_path,
            "language": language,
            "repo_name": repo_name or "",
            "repo_path": workspace,
        }
        content = build_content(EventType.FILE_SAVE, raw)

    elif event_type == EventType.FILE_CREATE:
        file_path = str(payload.get("file", ""))
        filename = str(payload.get("filename", "")) or os.path.basename(file_path)
        raw = {
            "filename": filename,
            "file_path": file_path,
            "repo_name": repo_name or "",
            "repo_path": workspace,
        }
        content = build_content(EventType.FILE_CREATE, raw)

    elif event_type == EventType.FILE_DELETE:
        file_path = str(payload.get("file", ""))
        filename = str(payload.get("filename", "")) or os.path.basename(file_path)
        raw = {
            "filename": filename,
            "file_path": file_path,
            "repo_name": repo_name or "",
            "repo_path": workspace,
        }
        content = build_content(EventType.FILE_DELETE, raw)

    elif event_type == EventType.FILE_RENAME:
        old_file = str(payload.get("old_file", ""))
        new_file = str(payload.get("new_file", ""))
        old_filename = str(payload.get("old_filename", "")) or os.path.basename(old_file)
        new_filename = str(payload.get("new_filename", "")) or os.path.basename(new_file)
        raw = {
            "old_filename": old_filename,
            "new_filename": new_filename,
            "old_file": old_file,
            "new_file": new_file,
            "repo_name": repo_name or "",
            "repo_path": workspace,
        }
        content = build_content(EventType.FILE_RENAME, raw)

    elif event_type == EventType.DEBUG_SESSION:
        name = str(payload.get("name", ""))
        debug_type = str(payload.get("debug_type", ""))
        action = "started" if event_type_str == "debug_session_start" else "ended"
        raw = {
            "name": name,
            "debug_type": debug_type,
            "action": action,
            "repo_name": repo_name or "",
            "repo_path": workspace,
        }
        content = build_content(EventType.DEBUG_SESSION, raw)

    elif event_type == EventType.TEST_RUN:
        name = str(payload.get("name", ""))
        action = "started" if event_type_str == "test_run_start" else "finished"
        raw: dict = {
            "name": name,
            "action": action,
            "repo_name": repo_name or "",
            "repo_path": workspace,
        }
        if event_type_str == "test_run_finish":
            raw["exit_code"] = int(payload.get("exit_code", 0))
        content = build_content(EventType.TEST_RUN, raw)

    elif event_type == EventType.REPO_OPEN:
        raw = {
            "repo_name": repo_name or workspace,
            "repo_path": workspace,
            "event": event_type_str,
        }
        content = build_content(EventType.REPO_OPEN, raw)

    elif event_type == EventType.REPO_CLOSE:
        raw = {
            "repo_name": repo_name or workspace,
            "repo_path": workspace,
            "duration_minutes": 0,
        }
        content = build_content(EventType.REPO_CLOSE, raw)

    else:
        raw = dict(payload)
        content = f"[vscode] {event_type_str}"

    event = Event(
        timestamp=ts_out,
        date=date_str,
        event_type=event_type,
        source=Source.VSCODE_EXT,
        content=content,
        raw_data=raw,
        repo_path=repo_path,
        repo_name=repo_name,
    )

    try:
        event_callback(event)
    except Exception:
        logger.exception("Error in VS Code event callback")
