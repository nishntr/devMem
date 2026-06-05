"""Session detection — assigns work sessions to events based on idle windows."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Callable, Optional

from recall.models import Event

logger = logging.getLogger(__name__)

# Default idle window: if last event was more than this many minutes ago,
# start a new session.
DEFAULT_SESSION_IDLE_MINUTES = 30


class SessionDetector:
    """
    Assigns ``session_id`` UUIDs to events.

    A new session starts when the elapsed time since the previous event
    exceeds *session_idle_minutes*, or when the repo changes significantly.
    """

    def __init__(
        self,
        session_idle_minutes: int = DEFAULT_SESSION_IDLE_MINUTES,
        get_latest_session: Callable[[], Optional[dict]] = lambda: None,
        upsert_session: Callable[..., None] = lambda *a, **k: None,
    ) -> None:
        self._idle_minutes = session_idle_minutes
        self._get_latest_session = get_latest_session
        self._upsert_session = upsert_session

        # In-memory state — populated lazily from DB on first use
        self._current_session_id: Optional[str] = None
        self._current_session_start: Optional[str] = None
        self._last_event_ts: Optional[str] = None
        self._last_repo: Optional[str] = None
        self._loaded = False

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def assign(self, event: Event) -> Event:
        """
        Set event.session_id.  Returns the (mutated) event.
        """
        self._maybe_load_from_db()

        event_ts = self._parse_ts(event.timestamp)

        if self._should_start_new_session(event_ts, event.repo_path):
            self._start_new_session(event.timestamp, event.repo_path, event.repo_name)
        else:
            # Extend existing session
            if self._current_session_id:
                self._upsert_session(
                    self._current_session_id,
                    self._current_session_start,
                    end_time=event.timestamp,
                    repo_path=event.repo_path,
                    primary_repo=event.repo_name,
                )

        event.session_id = self._current_session_id
        self._last_event_ts = event.timestamp
        self._last_repo = event.repo_path
        return event

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _maybe_load_from_db(self) -> None:
        """Load session state from DB on first call (to survive daemon restarts)."""
        if self._loaded:
            return
        self._loaded = True
        try:
            session = self._get_latest_session()
            if session:
                self._current_session_id = session["id"]
                self._current_session_start = session["start_time"]
                self._last_event_ts = session.get("end_time") or session["start_time"]
                self._last_repo = session.get("repo_path")
        except Exception:
            logger.exception("Could not load latest session from DB")

    def _should_start_new_session(
        self,
        event_ts: Optional[datetime],
        repo_path: Optional[str],
    ) -> bool:
        if self._current_session_id is None:
            return True
        if self._last_event_ts is None:
            return True
        if event_ts is None:
            return False

        last_ts = self._parse_ts(self._last_event_ts)
        if last_ts is None:
            return True

        elapsed_minutes = (event_ts - last_ts).total_seconds() / 60
        if elapsed_minutes > self._idle_minutes:
            return True

        return False

    def _start_new_session(
        self,
        ts: str,
        repo_path: Optional[str],
        repo_name: Optional[str],
    ) -> None:
        session_id = str(uuid.uuid4())
        self._current_session_id = session_id
        self._current_session_start = ts
        try:
            self._upsert_session(
                session_id,
                ts,
                end_time=None,
                repo_path=repo_path,
                primary_repo=repo_name,
                event_count=0,
            )
        except Exception:
            logger.exception("upsert_session failed")

    @staticmethod
    def _parse_ts(ts_str: str) -> Optional[datetime]:
        """Parse ISO 8601 timestamp to timezone-aware datetime."""
        try:
            return datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            return None
