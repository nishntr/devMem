"""Process and port tracker using psutil.

Polls every 30 s to detect:
  - Dev server processes starting/stopping (by process name allowlist)
  - Listening TCP ports opening/closing in the dev-server range

Emits PORT_OPEN / PORT_CLOSE events when a listening port appears or
disappears for a tracked process.

Gracefully becomes a no-op when psutil is not installed.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Callable, Optional

from recall.models import Event, EventType, Source

logger = logging.getLogger(__name__)

try:
    import psutil  # type: ignore

    _PSUTIL_AVAILABLE = True
except ImportError:
    _PSUTIL_AVAILABLE = False

# Process names that indicate a dev server or interesting background service.
# Matched as a substring of the process name or first cmdline argument.
_DEV_PROCESS_NAMES = {
    "node",
    "npm",
    "npx",
    "yarn",
    "pnpm",
    "bun",
    "deno",
    "next",
    "vite",
    "webpack",
    "parcel",
    "rollup",
    "esbuild",
    "turbopack",
    "python",
    "python3",
    "uvicorn",
    "gunicorn",
    "hypercorn",
    "fastapi",
    "flask",
    "django",
    "starlette",
    "tornado",
    "aiohttp",
    "ruby",
    "rails",
    "puma",
    "unicorn",
    "thin",
    "java",
    "mvn",
    "gradle",
    "go",
    "air",
    "gin",
    "fiber",
    "php",
    "artisan",
    "symfony",
    "cargo",
    "rust-analyzer",
    "dotnet",
}

# Only track ports in this range (typical dev / ephemeral service ports).
_PORT_MIN = 1024
_PORT_MAX = 65535

# Ports to ignore (databases, system services, etc.)
_PORT_IGNORE = {
    3306,   # MySQL
    5432,   # PostgreSQL
    6379,   # Redis
    27017,  # MongoDB
    11211,  # Memcached
    2181,   # ZooKeeper
    2375,   # Docker daemon (unencrypted)
    2376,   # Docker daemon (TLS)
    22,     # SSH
    25,     # SMTP
    53,     # DNS
    80,     # HTTP (production)
    443,    # HTTPS (production)
}

_POLL_INTERVAL = 30  # seconds


def _now_ts() -> tuple[str, str]:
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%SZ"), now.strftime("%Y-%m-%d")


def _is_dev_process(proc: "psutil.Process") -> bool:  # type: ignore[name-defined]
    """Return True if the process looks like a dev server."""
    try:
        name = proc.name().lower()
        if name in _DEV_PROCESS_NAMES:
            return True
        # Also check the first cmdline argument (e.g. "python manage.py runserver")
        cmdline = proc.cmdline()
        if cmdline:
            exe_base = cmdline[0].rsplit("/", 1)[-1].lower()
            if exe_base in _DEV_PROCESS_NAMES:
                return True
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass
    return False


def _snapshot_ports() -> dict[int, str]:
    """Return {port: process_name} for all listening TCP sockets owned by dev processes."""
    result: dict[int, str] = {}
    try:
        connections = psutil.net_connections(kind="tcp")
    except psutil.AccessDenied:
        return result

    for conn in connections:
        if conn.status != psutil.CONN_LISTEN:
            continue
        port = conn.laddr.port
        if port < _PORT_MIN or port > _PORT_MAX or port in _PORT_IGNORE:
            continue
        pid = conn.pid
        if pid is None:
            continue
        try:
            proc = psutil.Process(pid)
            if _is_dev_process(proc):
                result[port] = proc.name()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return result


class ProcessCollector:
    """Poll psutil every 30 s and emit PORT_OPEN / PORT_CLOSE events."""

    def __init__(self, event_callback: Callable[[Event], None]) -> None:
        self._cb = event_callback
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    def start(self) -> None:
        if not _PSUTIL_AVAILABLE:
            logger.debug("psutil unavailable — process/port tracking disabled")
            return

        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name="dev-recall-process",
        )
        self._thread.start()
        logger.info("ProcessCollector started (poll interval %ds)", _POLL_INTERVAL)

    def stop(self) -> None:
        self._stop_event.set()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _run(self) -> None:
        prev: dict[int, str] = {}
        try:
            prev = _snapshot_ports()
        except Exception:
            logger.exception("ProcessCollector initial snapshot failed")

        while not self._stop_event.wait(_POLL_INTERVAL):
            try:
                curr = _snapshot_ports()
                self._diff(prev, curr)
                prev = curr
            except Exception:
                logger.exception("ProcessCollector poll failed")

    def _diff(self, prev: dict[int, str], curr: dict[int, str]) -> None:
        ts, date = _now_ts()

        # Newly opened ports
        for port, proc_name in curr.items():
            if port not in prev:
                raw = {"port": port, "process_name": proc_name}
                event = Event(
                    timestamp=ts,
                    date=date,
                    event_type=EventType.PORT_OPEN,
                    source=Source.PROCESS_TRACKER,
                    content="",
                    raw_data=raw,
                )
                from recall.models import build_content

                event.content = build_content(EventType.PORT_OPEN, raw)
                self._cb(event)

        # Closed ports
        for port, proc_name in prev.items():
            if port not in curr:
                raw = {"port": port, "process_name": proc_name}
                event = Event(
                    timestamp=ts,
                    date=date,
                    event_type=EventType.PORT_CLOSE,
                    source=Source.PROCESS_TRACKER,
                    content="",
                    raw_data=raw,
                )
                from recall.models import build_content

                event.content = build_content(EventType.PORT_CLOSE, raw)
                self._cb(event)
