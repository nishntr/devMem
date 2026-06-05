"""Recall background daemon — orchestrates all collectors and background threads."""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from recall.config import Config
from recall.models import Event

logger = logging.getLogger(__name__)

_SYSTEMD_UNIT_TEMPLATE = """\
[Unit]
Description=Recall developer activity daemon
After=default.target

[Service]
Type=simple
ExecStart={recall_bin} daemon start --foreground
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
"""

_DAILY_SUMMARIZER_HOUR = 23
_DAILY_SUMMARIZER_MINUTE = 30


class Daemon:
    """
    The main daemon process.

    Responsibilities:
      - Runs Flask HTTP server for VS Code extension events
      - Runs watchdog observers for shell.tsv and git.tsv
      - Runs AI chat log watcher
      - Runs background threads: embedder, git poller, daily summarizer,
        retention cleaner
    """

    def __init__(self, config: Config) -> None:
        self._config = config
        self._stop_event = threading.Event()

        # Lazy imports to keep startup fast
        from recall.storage.db import DB
        from recall.storage.vectors import VectorStore
        from recall.processor.embedder import EmbedderQueue
        from recall.processor.enricher import Enricher
        from recall.processor.session import SessionDetector

        self._db = DB(config.db_path)
        self._vectors = VectorStore.from_file(config.faiss_path, dim=config.embedding_dim)

        # Embedder
        self._embedder = EmbedderQueue(
            db=self._db,
            vectors=self._vectors,
            model_name=config.embedding_model,
            faiss_save_path=str(config.faiss_path),
            get_next_embedding_id=lambda: int(self._db.get_kv("next_embedding_id") or 1),
            set_next_embedding_id=lambda v: self._db.set_kv("next_embedding_id", str(v)),
        )

        # Enricher
        privacy = config.privacy
        self._enricher = Enricher(
            cmd_ignore_patterns=privacy.get("cmd_ignore_patterns", []),
            file_ignore_patterns=privacy.get("file_ignore_patterns", []),
            repo_ignore_patterns=privacy.get("repo_ignore_patterns", []),
            upsert_repo=self._db.upsert_repo,
        )

        # Session detector
        self._session_detector = SessionDetector(
            session_idle_minutes=config.search.get("session_idle_minutes", 30),
            get_latest_session=self._db.get_latest_session,
            upsert_session=self._db.upsert_session,
        )

        # Collectors (initialised in start())
        self._shell_collector = None
        self._git_collector = None
        self._ai_collector = None
        self._flask_thread = None
        self._window_collector = None
        self._session_event_collector = None
        self._container_collector = None
        self._process_collector = None

    # ------------------------------------------------------------------
    # Event pipeline
    # ------------------------------------------------------------------

    def _handle_raw_event(self, event: Event) -> None:
        """
        Called by all collectors for each raw event.
        Runs enrichment → session → DB insert → embedder queue.
        """
        enriched = self._enricher.enrich(event)
        if enriched is None:
            return  # dropped by privacy filter

        enriched = self._session_detector.assign(enriched)

        try:
            event_id = self._db.insert_event(enriched)
        except Exception:
            logger.exception("DB insert_event failed")
            return

        enriched.id = event_id
        self._embedder.enqueue([event_id])

    # ------------------------------------------------------------------
    # Start / stop
    # ------------------------------------------------------------------

    def start(self, foreground: bool = False) -> None:
        """Start the daemon (blocks if foreground=True)."""
        logger.info("Recall daemon starting (PID %d)", os.getpid())

        if not foreground:
            self._write_pid()

        # Start embedder worker
        self._embedder.start()

        # Embed anything that wasn't embedded before (crash recovery)
        threading.Thread(
            target=self._embedder.embed_pending,
            daemon=True,
            name="dev-recall-embed-pending",
        ).start()

        # Shell collector
        from recall.collectors.shell import ShellCollector

        self._shell_collector = ShellCollector(
            shell_tsv=self._config.shell_tsv_path,
            cmd_ignore_patterns=self._config.privacy.get("cmd_ignore_patterns", []),
            event_callback=self._handle_raw_event,
            get_offset=lambda: self._db.get_kv("shell_tsv_offset"),
            set_offset=lambda v: self._db.set_kv("shell_tsv_offset", v),
        )
        self._shell_collector.start()

        # Git collector
        from recall.collectors.git import GitCollector

        self._git_collector = GitCollector(
            git_tsv=self._config.git_tsv_path,
            event_callback=self._handle_raw_event,
            get_kv=self._db.get_kv,
            set_kv=self._db.set_kv,
            repo_ignore_patterns=self._config.privacy.get("repo_ignore_patterns", []),
        )
        self._git_collector.start()

        # AI chat collector
        if self._config.capture.get("ai_chat", True):
            from recall.collectors.ai_chat import AIChatCollector

            self._ai_collector = AIChatCollector(
                event_callback=self._handle_raw_event,
                get_kv=self._db.get_kv,
                set_kv=self._db.set_kv,
                ai_chat_max_chars=self._config.privacy.get("ai_chat_max_chars", 200),
            )
            self._ai_collector.start()

        # Flask HTTP server for VS Code extension
        if self._config.capture.get("vscode", True):
            self._flask_thread = threading.Thread(
                target=self._run_flask,
                daemon=True,
                name="dev-recall-flask",
            )
            self._flask_thread.start()

        # Window tracking (Linux X11/XWayland via libwnck, optional)
        if self._config.capture.get("window_tracking", True):
            from recall.collectors.linux_window import LinuxWindowCollector

            self._window_collector = LinuxWindowCollector(event_callback=self._handle_raw_event)
            self._window_collector.start()

        # Session events (Linux D-Bus logind, optional)
        if self._config.capture.get("session_events", True):
            from recall.collectors.linux_session import LinuxSessionCollector

            self._session_event_collector = LinuxSessionCollector(
                event_callback=self._handle_raw_event
            )
            self._session_event_collector.start()

        # Container events (Docker/Podman, optional)
        if self._config.capture.get("container_events", True):
            from recall.collectors.containers import ContainerCollector

            self._container_collector = ContainerCollector(event_callback=self._handle_raw_event)
            self._container_collector.start()

        # Process/port tracking (psutil, optional)
        if self._config.capture.get("process_tracking", True):
            from recall.collectors.linux_process import ProcessCollector

            self._process_collector = ProcessCollector(event_callback=self._handle_raw_event)
            self._process_collector.start()

        # Daily summarizer
        threading.Thread(
            target=self._daily_summarizer_loop,
            daemon=True,
            name="devmem-summarizer",
        ).start()

        # Retention cleaner
        threading.Thread(
            target=self._retention_cleaner_loop,
            daemon=True,
            name="devmem-retention",
        ).start()

        # Signal handlers for graceful shutdown
        signal.signal(signal.SIGTERM, self._on_signal)
        signal.signal(signal.SIGINT, self._on_signal)

        logger.info("Recall daemon running")

        if foreground:
            self._stop_event.wait()
            self._shutdown()

    def stop(self) -> None:
        self._stop_event.set()

    def _shutdown(self) -> None:
        logger.info("Recall daemon shutting down…")
        if self._shell_collector:
            self._shell_collector.stop()
        if self._git_collector:
            self._git_collector.stop()
        if self._ai_collector:
            self._ai_collector.stop()
        if self._window_collector:
            self._window_collector.stop()
        if self._session_event_collector:
            self._session_event_collector.stop()
        if self._container_collector:
            self._container_collector.stop()
        if self._process_collector:
            self._process_collector.stop()
        self._embedder.stop()
        # Final FAISS save
        try:
            self._vectors.save(self._config.faiss_path)
        except Exception:
            logger.exception("Final FAISS save failed")
        self._db.close()
        self._remove_pid()
        logger.info("Recall daemon stopped")

    def _on_signal(self, signum, frame) -> None:
        logger.info("Received signal %d — stopping", signum)
        self._stop_event.set()

    # ------------------------------------------------------------------
    # Flask HTTP server
    # ------------------------------------------------------------------

    def _run_flask(self) -> None:
        from flask import Flask, request, jsonify
        from recall.collectors.vscode import parse_vscode_event

        app = Flask(__name__)

        @app.route("/event", methods=["POST"])
        def receive_event():
            data = request.get_json(silent=True) or {}
            parse_vscode_event(data, self._handle_raw_event)
            return jsonify({"ok": True}), 200

        @app.route("/status", methods=["GET"])
        def status():
            return jsonify({
                "status": "running",
                "pid": os.getpid(),
                "events": self._db.get_event_count(),
                "vectors": self._vectors.size(),
            }), 200

        import logging as _log
        _log.getLogger("werkzeug").setLevel(logging.WARNING)

        app.run(
            host="127.0.0.1",
            port=self._config.daemon_port,
            threaded=True,
            use_reloader=False,
        )

    # ------------------------------------------------------------------
    # Daily summarizer
    # ------------------------------------------------------------------

    def _daily_summarizer_loop(self) -> None:
        while not self._stop_event.is_set():
            self._stop_event.wait(60)  # check every minute
            if self._stop_event.is_set():
                break
            now = datetime.now(timezone.utc)
            if now.hour == _DAILY_SUMMARIZER_HOUR and now.minute == _DAILY_SUMMARIZER_MINUTE:
                if self._config.summary.get("auto_generate", True):
                    self._generate_daily_summary(now.strftime("%Y-%m-%d"))
                # Sleep 2 min so we don't fire twice in the same minute
                self._stop_event.wait(120)

    def _generate_daily_summary(self, date: str) -> None:
        existing = self._db.get_daily_summary(date)
        if existing:
            return  # already generated

        events = self._db.get_events_by_date(date)
        if not events:
            return

        from recall.query.context import build_prompt_summary
        from recall.query.llm import ask as llm_ask, is_available

        if not is_available():
            logger.debug("No LLM key — skipping daily summary for %s", date)
            return

        try:
            messages = build_prompt_summary(date, events)
            summary = llm_ask(messages)
            repos = list({e.repo_name for e in events if e.repo_name})
            highlights = [e.content for e in events if "commit" in e.event_type.value][:10]
            self._db.upsert_daily_summary(date, summary, repos, highlights, len(events))
            logger.info("Generated daily summary for %s", date)
        except Exception:
            logger.exception("Daily summary generation failed for %s", date)

    # ------------------------------------------------------------------
    # Retention cleaner
    # ------------------------------------------------------------------

    def _retention_cleaner_loop(self) -> None:
        """Run once per day to delete events older than retention_days."""
        # Run once on startup, then every 24 hours
        self._run_retention_cleanup()
        while not self._stop_event.is_set():
            self._stop_event.wait(3600 * 24)
            if self._stop_event.is_set():
                break
            self._run_retention_cleanup()

    def _run_retention_cleanup(self) -> None:
        from datetime import timedelta

        retention_days = self._config.retention_days
        cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).strftime("%Y-%m-%d")
        try:
            n = self._db.delete_events_before(cutoff)
            if n:
                logger.info("Retention cleaner deleted %d events before %s", n, cutoff)
        except Exception:
            logger.exception("Retention cleaner failed")

    # ------------------------------------------------------------------
    # PID file
    # ------------------------------------------------------------------

    def _write_pid(self) -> None:
        try:
            self._config.pid_path.parent.mkdir(parents=True, exist_ok=True)
            self._config.pid_path.write_text(str(os.getpid()))
        except OSError as exc:
            logger.warning("Could not write PID file: %s", exc)

    def _remove_pid(self) -> None:
        try:
            self._config.pid_path.unlink(missing_ok=True)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Process management helpers (used by CLI)
# ---------------------------------------------------------------------------


def read_pid(config: Config) -> Optional[int]:
    """Return the daemon PID from the PID file, or None."""
    try:
        pid_str = config.pid_path.read_text().strip()
        return int(pid_str)
    except (OSError, ValueError):
        return None


def is_running(config: Config) -> bool:
    """Return True if the daemon process is alive."""
    pid = read_pid(config)
    if pid is None:
        return False
    try:
        os.kill(pid, 0)  # signal 0: just check existence
        return True
    except (ProcessLookupError, PermissionError):
        return False


def stop_daemon(config: Config) -> bool:
    """Send SIGTERM to the daemon. Returns True if the signal was sent."""
    pid = read_pid(config)
    if pid is None:
        return False
    try:
        os.kill(pid, signal.SIGTERM)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def write_systemd_unit(config: Config) -> Path:
    """Write the systemd user unit file and return its path."""
    import shutil

    recall_bin = shutil.which("recall") or sys.executable + " -m devmem"
    unit_content = _SYSTEMD_UNIT_TEMPLATE.format(recall_bin=recall_bin)

    unit_dir = Path.home() / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    unit_path = unit_dir / "dev-recall.service"
    unit_path.write_text(unit_content)
    return unit_path


def start_daemon_background(config: Config) -> int:
    """Fork the daemon to the background and return its PID."""
    import subprocess

    log_fh = open(str(config.log_path), "a")  # noqa: SIM115
    proc = subprocess.Popen(
        [sys.executable, "-m", "recall.daemon_main"],
        start_new_session=True,
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        env={**os.environ, "DEVMEM_FOREGROUND": "1"},
    )
    log_fh.close()  # child inherits the fd; parent can close
    # Wait briefly to ensure the process started
    time.sleep(0.5)
    return proc.pid
