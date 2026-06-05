"""Docker / Podman container event collector.

Streams Docker events filtered to start/die/create/destroy and emits
CONTAINER_START / CONTAINER_STOP events.

Supports both Docker (default socket) and Podman (rootless socket at
/run/user/<uid>/podman/podman.sock) by checking both sockets.

Gracefully becomes a no-op when:
  - The `docker` SDK is not installed
  - No accessible Docker/Podman socket is found
"""

from __future__ import annotations

import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from recall.models import Event, EventType, Source

logger = logging.getLogger(__name__)

try:
    import docker  # type: ignore

    _DOCKER_SDK_AVAILABLE = True
except ImportError:
    _DOCKER_SDK_AVAILABLE = False

# Canonical socket paths in preference order
_DOCKER_SOCKETS = [
    "/var/run/docker.sock",
    "/run/docker.sock",
]


def _podman_socket() -> Optional[str]:
    uid = os.getuid()
    p = Path(f"/run/user/{uid}/podman/podman.sock")
    return str(p) if p.exists() else None


def _find_socket() -> Optional[str]:
    for s in _DOCKER_SOCKETS:
        if os.path.exists(s) and os.access(s, os.R_OK | os.W_OK):
            return s
    ps = _podman_socket()
    if ps and os.access(ps, os.R_OK | os.W_OK):
        return ps
    return None


def _now_ts() -> tuple[str, str]:
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%SZ"), now.strftime("%Y-%m-%d")


# Container actions that map to CONTAINER_START
_START_ACTIONS = {"start", "restart"}
# Container actions that map to CONTAINER_STOP
_STOP_ACTIONS = {"die", "stop", "destroy", "kill"}


class ContainerCollector:
    """Stream Docker/Podman events and emit container lifecycle events."""

    def __init__(self, event_callback: Callable[[Event], None]) -> None:
        self._cb = event_callback
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._client: Optional["docker.DockerClient"] = None  # type: ignore[name-defined]

    def start(self) -> None:
        if not _DOCKER_SDK_AVAILABLE:
            logger.debug("docker SDK unavailable — container tracking disabled")
            return

        socket = _find_socket()
        if socket is None:
            logger.debug("No accessible Docker/Podman socket — container tracking disabled")
            return

        try:
            self._client = docker.DockerClient(base_url=f"unix://{socket}")
            # Quick ping to verify it's alive
            self._client.ping()
        except Exception as exc:
            logger.debug("Docker/Podman socket not responding (%s) — disabled", exc)
            self._client = None
            return

        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name="devmem-containers",
        )
        self._thread.start()
        logger.info("ContainerCollector started on socket %s", socket)

    def stop(self) -> None:
        self._stop_event.set()
        # Close client to unblock the blocking events() generator
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _run(self) -> None:
        if self._client is None:
            return
        try:
            for raw in self._client.events(
                decode=True,
                filters={"type": "container"},
            ):
                if self._stop_event.is_set():
                    break
                self._handle_raw(raw)
        except Exception:
            if not self._stop_event.is_set():
                logger.exception("ContainerCollector stream crashed")

    def _handle_raw(self, raw: dict) -> None:
        action = raw.get("Action", "")
        attrs = raw.get("Actor", {}).get("Attributes", {})
        container_id = raw.get("Actor", {}).get("ID", "")[:12]
        name = attrs.get("name", container_id)
        image = attrs.get("image", "")

        if action in _START_ACTIONS:
            event_type = EventType.CONTAINER_START
        elif action in _STOP_ACTIONS:
            event_type = EventType.CONTAINER_STOP
        else:
            return

        ts, date = _now_ts()
        event_data = {
            "name": name,
            "container_id": container_id,
            "image": image,
            "action": action,
        }
        event = Event(
            timestamp=ts,
            date=date,
            event_type=event_type,
            source=Source.CONTAINER_EVENTS,
            content="",
            raw_data=event_data,
        )
        from recall.models import build_content

        event.content = build_content(event_type, event_data)
        self._cb(event)
