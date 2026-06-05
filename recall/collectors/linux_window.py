"""Linux window/app tracker using libwnck (X11/XWayland).

Tracks application open/close, window focus changes, and virtual workspace
switches via Wnck signals on a GLib main loop in a daemon thread.

Gracefully becomes a no-op when:
  - PyGObject / libwnck is not installed
  - No X display is available (pure Wayland without XWayland)
  - Wnck.Screen.get_default() returns None
"""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Callable, Optional

from recall.models import Event, EventType, Source

logger = logging.getLogger(__name__)

try:
    import gi

    gi.require_version("Wnck", "3.0")
    gi.require_version("GLib", "2.0")
    from gi.repository import GLib, Wnck  # type: ignore

    _WNCK_AVAILABLE = True
except Exception:
    _WNCK_AVAILABLE = False


def _now_ts() -> tuple[str, str]:
    """Return (ISO timestamp, YYYY-MM-DD)."""
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%SZ"), now.strftime("%Y-%m-%d")


class LinuxWindowCollector:
    """Collect application and window events from the X11/XWayland session."""

    def __init__(self, event_callback: Callable[[Event], None]) -> None:
        self._cb = event_callback
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional["GLib.MainLoop"] = None  # type: ignore[name-defined]
        # Track app open times for duration calculation
        self._app_open_times: dict[int, float] = {}
        # Track active workspace name for switch events
        self._current_workspace: Optional[str] = None

    def start(self) -> None:
        if not _WNCK_AVAILABLE:
            logger.debug("libwnck unavailable — window tracking disabled")
            return

        display = os.environ.get("DISPLAY", "")
        if not display:
            logger.debug("No DISPLAY — window tracking disabled")
            return

        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name="devmem-window",
        )
        self._thread.start()

    def stop(self) -> None:
        if self._loop is not None:
            self._loop.quit()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _run(self) -> None:
        try:
            self._loop = GLib.MainLoop()

            # Wnck.Screen.get_default() must be called inside the GLib loop thread
            GLib.idle_add(self._setup_wnck)
            self._loop.run()
        except Exception:
            logger.exception("LinuxWindowCollector loop crashed")

    def _setup_wnck(self) -> bool:
        try:
            screen = Wnck.Screen.get_default()
            if screen is None:
                logger.debug("Wnck.Screen.get_default() returned None — disabling window tracking")
                if self._loop:
                    self._loop.quit()
                return False

            screen.force_update()

            # Seed current workspace
            ws = screen.get_active_workspace()
            self._current_workspace = ws.get_name() if ws else None

            screen.connect("application-opened", self._on_app_opened)
            screen.connect("application-closed", self._on_app_closed)
            screen.connect("active-window-changed", self._on_window_focus)
            screen.connect("active-workspace-changed", self._on_workspace_changed)
            logger.info("Wnck window tracking active on DISPLAY=%s", os.environ.get("DISPLAY"))
        except Exception:
            logger.exception("Wnck setup failed")
        return False  # remove idle callback

    def _on_app_opened(self, screen: "Wnck.Screen", app: "Wnck.Application") -> None:  # type: ignore[name-defined]
        xid = app.get_xid()
        self._app_open_times[xid] = time.monotonic()
        ts, date = _now_ts()
        name = app.get_name() or ""
        event = Event(
            timestamp=ts,
            date=date,
            event_type=EventType.APP_OPEN,
            source=Source.LINUX_WINDOW,
            content="",
            raw_data={"app_name": name, "xid": xid},
        )
        from recall.models import build_content

        event.content = build_content(EventType.APP_OPEN, event.raw_data)
        self._cb(event)

    def _on_app_closed(self, screen: "Wnck.Screen", app: "Wnck.Application") -> None:  # type: ignore[name-defined]
        xid = app.get_xid()
        ts, date = _now_ts()
        name = app.get_name() or ""
        raw: dict = {"app_name": name, "xid": xid}
        open_t = self._app_open_times.pop(xid, None)
        if open_t is not None:
            raw["duration_seconds"] = round(time.monotonic() - open_t)
        event = Event(
            timestamp=ts,
            date=date,
            event_type=EventType.APP_CLOSE,
            source=Source.LINUX_WINDOW,
            content="",
            raw_data=raw,
        )
        from recall.models import build_content

        event.content = build_content(EventType.APP_CLOSE, event.raw_data)
        self._cb(event)

    def _on_window_focus(
        self,
        screen: "Wnck.Screen",  # type: ignore[name-defined]
        prev_window: Optional["Wnck.Window"],  # type: ignore[name-defined]
    ) -> None:
        window = screen.get_active_window()
        if window is None:
            return
        ts, date = _now_ts()
        title = window.get_name() or ""
        app = window.get_application()
        app_name = app.get_name() if app else ""
        event = Event(
            timestamp=ts,
            date=date,
            event_type=EventType.WINDOW_FOCUS,
            source=Source.LINUX_WINDOW,
            content="",
            raw_data={"window_title": title, "app_name": app_name},
        )
        from recall.models import build_content

        event.content = build_content(EventType.WINDOW_FOCUS, event.raw_data)
        self._cb(event)

    def _on_workspace_changed(
        self,
        screen: "Wnck.Screen",  # type: ignore[name-defined]
        prev_ws: Optional["Wnck.Workspace"],  # type: ignore[name-defined]
    ) -> None:
        from_name = prev_ws.get_name() if prev_ws else (self._current_workspace or "")
        ws = screen.get_active_workspace()
        to_name = ws.get_name() if ws else ""
        self._current_workspace = to_name
        ts, date = _now_ts()
        event = Event(
            timestamp=ts,
            date=date,
            event_type=EventType.WORKSPACE_SWITCH,
            source=Source.LINUX_WINDOW,
            content="",
            raw_data={"from_workspace": from_name, "to_workspace": to_name},
        )
        from recall.models import build_content

        event.content = build_content(EventType.WORKSPACE_SWITCH, event.raw_data)
        self._cb(event)
