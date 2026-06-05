"""Linux session event collector via D-Bus logind.

Subscribes to org.freedesktop.login1.Manager signals:
  - PrepareForSleep(before: bool)  → SYSTEM_SUSPEND / SYSTEM_RESUME
  - org.freedesktop.login1.Session Lock / Unlock → SESSION_LOCK / SESSION_UNLOCK

Uses dbus-next with an asyncio event loop running in a daemon thread.

Gracefully becomes a no-op when dbus-next is not installed or the D-Bus
session bus is not available.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import threading
from datetime import datetime, timezone
from typing import Callable, Optional

from recall.models import Event, EventType, Source

logger = logging.getLogger(__name__)

try:
    from dbus_next.aio import MessageBus  # type: ignore
    from dbus_next import BusType, Message, MessageType  # type: ignore

    _DBUS_AVAILABLE = True
except ImportError:
    _DBUS_AVAILABLE = False


def _now_ts() -> tuple[str, str]:
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%SZ"), now.strftime("%Y-%m-%d")


class LinuxSessionCollector:
    """Collect screen lock/unlock and system sleep/resume events from D-Bus."""

    def __init__(self, event_callback: Callable[[Event], None]) -> None:
        self._cb = event_callback
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._lock_time: Optional[float] = None
        self._suspend_time: Optional[float] = None

    def start(self) -> None:
        if not _DBUS_AVAILABLE:
            logger.debug("dbus-next unavailable — session event tracking disabled")
            return

        # Only works with a session D-Bus socket
        if not os.environ.get("DBUS_SESSION_BUS_ADDRESS") and not os.path.exists(
            "/run/user/%d/bus" % os.getuid()
        ):
            logger.debug("No D-Bus session bus — session event tracking disabled")
            return

        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name="devmem-session",
        )
        self._thread.start()

    def stop(self) -> None:
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._loop.stop)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        try:
            self._loop.run_until_complete(self._subscribe())
        except Exception:
            logger.exception("LinuxSessionCollector crashed")

    async def _subscribe(self) -> None:
        try:
            bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        except Exception:
            logger.debug("Could not connect to system D-Bus — session tracking disabled")
            return

        # ----------------------------------------------------------------
        # Subscribe to PrepareForSleep (system suspend/resume)
        # ----------------------------------------------------------------
        await bus.call(
            Message(
                destination="org.freedesktop.DBus",
                path="/org/freedesktop/DBus",
                interface="org.freedesktop.DBus",
                member="AddMatch",
                signature="s",
                body=[
                    "type='signal',"
                    "sender='org.freedesktop.login1',"
                    "interface='org.freedesktop.login1.Manager',"
                    "member='PrepareForSleep'"
                ],
            )
        )

        # ----------------------------------------------------------------
        # Subscribe to session Lock/Unlock
        # ----------------------------------------------------------------
        await bus.call(
            Message(
                destination="org.freedesktop.DBus",
                path="/org/freedesktop/DBus",
                interface="org.freedesktop.DBus",
                member="AddMatch",
                signature="s",
                body=[
                    "type='signal',"
                    "sender='org.freedesktop.login1',"
                    "interface='org.freedesktop.login1.Session',"
                    "member='Lock'"
                ],
            )
        )
        await bus.call(
            Message(
                destination="org.freedesktop.DBus",
                path="/org/freedesktop/DBus",
                interface="org.freedesktop.DBus",
                member="AddMatch",
                signature="s",
                body=[
                    "type='signal',"
                    "sender='org.freedesktop.login1',"
                    "interface='org.freedesktop.login1.Session',"
                    "member='Unlock'"
                ],
            )
        )

        logger.info("LinuxSessionCollector subscribed to D-Bus logind signals")

        bus.add_message_handler(self._handle_message)

        # Keep running until stop() is called
        await asyncio.get_event_loop().run_in_executor(None, self._thread.join if self._thread else lambda: None)

    def _handle_message(self, message: "Message") -> None:  # type: ignore[name-defined]
        if message.message_type != MessageType.SIGNAL:
            return
        member = message.member
        if member == "PrepareForSleep":
            before = message.body[0] if message.body else True
            self._on_prepare_for_sleep(before)
        elif member == "Lock":
            self._on_session_lock()
        elif member == "Unlock":
            self._on_session_unlock()

    def _on_prepare_for_sleep(self, before: bool) -> None:
        ts, date = _now_ts()
        if before:
            # Going to sleep
            self._suspend_time = time.monotonic()
            event = Event(
                timestamp=ts,
                date=date,
                event_type=EventType.SYSTEM_SUSPEND,
                source=Source.LINUX_SESSION,
                content="",
                raw_data={},
            )
        else:
            # Waking up
            raw: dict = {}
            if self._suspend_time is not None:
                raw["suspended_seconds"] = round(time.monotonic() - self._suspend_time)
                self._suspend_time = None
            event = Event(
                timestamp=ts,
                date=date,
                event_type=EventType.SYSTEM_RESUME,
                source=Source.LINUX_SESSION,
                content="",
                raw_data=raw,
            )
        from recall.models import build_content

        event.content = build_content(event.event_type, event.raw_data)
        self._cb(event)

    def _on_session_lock(self) -> None:
        self._lock_time = time.monotonic()
        ts, date = _now_ts()
        event = Event(
            timestamp=ts,
            date=date,
            event_type=EventType.SESSION_LOCK,
            source=Source.LINUX_SESSION,
            content="",
            raw_data={},
        )
        from recall.models import build_content

        event.content = build_content(EventType.SESSION_LOCK, event.raw_data)
        self._cb(event)

    def _on_session_unlock(self) -> None:
        ts, date = _now_ts()
        raw: dict = {}
        if self._lock_time is not None:
            raw["locked_seconds"] = round(time.monotonic() - self._lock_time)
            self._lock_time = None
        event = Event(
            timestamp=ts,
            date=date,
            event_type=EventType.SESSION_UNLOCK,
            source=Source.LINUX_SESSION,
            content="",
            raw_data=raw,
        )
        from recall.models import build_content

        event.content = build_content(EventType.SESSION_UNLOCK, event.raw_data)
        self._cb(event)
