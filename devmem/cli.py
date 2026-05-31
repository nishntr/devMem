"""DevMem CLI — all commands."""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import click
from rich.console import Console
from rich.table import Table
from rich import box
from rich.text import Text

from devmem.config import load_config, save_config
from devmem.models import EventType

console = Console()
err_console = Console(stderr=True)

# Event type icon mapping
_ICONS = {
    "terminal_cmd": "⬢",
    "git_commit": "◆",
    "git_branch_switch": "⬡",
    "git_push": "▲",
    "git_merge": "⊕",
    "file_save": "✎",
    "file_create": "✚",
    "file_delete": "✖",
    "file_rename": "↔",
    "repo_open": "▶",
    "repo_close": "■",
    "ai_chat": "✦",
    "debug_session": "⏯",
    "test_run": "✓",
}
_TYPE_STYLES = {
    "terminal_cmd": "cyan",
    "git_commit": "green",
    "git_branch_switch": "yellow",
    "git_push": "bright_green",
    "git_merge": "green",
    "file_save": "blue",
    "file_create": "bright_blue",
    "file_delete": "red",
    "file_rename": "blue",
    "repo_open": "magenta",
    "repo_close": "dim magenta",
    "ai_chat": "bright_yellow",
    "debug_session": "bright_red",
    "test_run": "bright_green",
}


# ---------------------------------------------------------------------------
# CLI group
# ---------------------------------------------------------------------------


@click.group()
@click.version_option()
def cli():
    """DevMem — local-first developer memory layer."""
    pass


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------


@cli.command()
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompts")
def init(yes: bool):
    """Set up DevMem: create dirs, install hooks, start daemon."""
    config = load_config()

    console.rule("[bold]DevMem — Developer Memory Layer[/bold]")
    console.print()

    steps = [
        "Creating data directory",
        "Initializing database",
        "Installing shell hook",
        "Installing git hook",
        "Starting daemon",
        "VS Code extension (optional)",
    ]

    # Step 1: Create directories
    _print_step(1, steps[0])
    config.data_dir.mkdir(parents=True, exist_ok=True)
    config.config_dir.mkdir(parents=True, exist_ok=True)
    console.print(f"  → [green]✓[/green] {config.data_dir}")

    # Step 2: Init DB and FAISS
    _print_step(2, steps[1])
    from devmem.storage.db import DB
    from devmem.storage.vectors import VectorStore

    db = DB(config.db_path)
    db.close()
    vs = VectorStore(dim=config.embedding_dim)
    vs.save(config.faiss_path)
    console.print("  → [green]✓[/green] events.db + vectors.faiss")

    # Step 3: Shell hook
    _print_step(3, steps[2])
    _install_shell_hook(config)

    # Step 4: Git hooks
    _print_step(4, steps[3])
    _install_git_hooks(config)

    # Step 5: Start daemon
    _print_step(5, steps[4])
    _start_daemon(config)

    # Step 6: VS Code extension hint
    _print_step(6, steps[5])
    console.print("  → Run: [cyan]code --install-extension devmem.devmem-vscode[/cyan] (optional)")

    console.print()
    console.print("[bold green]Done. DevMem is running.[/bold green]")
    console.print()
    console.print("Try:")
    console.print("  [cyan]devmem today[/cyan]           — see today's activity")
    console.print('  [cyan]devmem ask "what did I work on?"[/cyan]')
    console.print("  [cyan]devmem timeline[/cyan]")


def _print_step(n: int, label: str):
    console.print(f"[dim]\\[{n}/6][/dim] {label}")


def _install_shell_hook(config) -> None:
    """Install shell hooks for zsh, bash, and fish unconditionally."""
    hooks = [
        ("zsh", Path(__file__).parent.parent / "shell" / "hook.zsh", config.hook_zsh_path, Path.home() / ".zshrc"),
        ("bash", Path(__file__).parent.parent / "shell" / "hook.bash", config.hook_bash_path, Path.home() / ".bashrc"),
        ("fish", Path(__file__).parent.parent / "shell" / "hook.fish", config.hook_fish_path, Path.home() / ".config" / "fish" / "config.fish"),
    ]

    installed: list[str] = []
    for shell_name, hook_src, hook_dst, rc_file in hooks:
        # Copy/write hook file
        if hook_src.exists():
            shutil.copy2(str(hook_src), str(hook_dst))
        else:
            _write_hook_from_package(config, shell_name)

        # Only add to rc file if the shell is actually installed
        if shell_name == "fish" and not shutil.which("fish"):
            continue

        # Append source line to rc file if not already present
        source_line = f"\n# DevMem shell hook\nsource \"{hook_dst}\"\n"
        rc_content = rc_file.read_text() if rc_file.exists() else ""
        if str(hook_dst) not in rc_content:
            rc_file.parent.mkdir(parents=True, exist_ok=True)
            with rc_file.open("a") as f:
                f.write(source_line)
            installed.append(shell_name)

    if installed:
        console.print(f"  → [green]✓[/green] Installed hooks for: {', '.join(installed)}")
        console.print("  → Run: [cyan]source ~/.zshrc[/cyan] or [cyan]source ~/.bashrc[/cyan]  (or open a new terminal)")
    else:
        console.print("  → [dim]Already installed[/dim]")


def _write_hook_from_package(config, shell_name: str) -> None:
    """Write a single hook file when the package is installed (no source tree available)."""
    from devmem._hooks import ZSH_HOOK, BASH_HOOK, FISH_HOOK  # type: ignore[import]

    if shell_name == "zsh":
        config.hook_zsh_path.parent.mkdir(parents=True, exist_ok=True)
        config.hook_zsh_path.write_text(ZSH_HOOK)
    elif shell_name == "bash":
        config.hook_bash_path.parent.mkdir(parents=True, exist_ok=True)
        config.hook_bash_path.write_text(BASH_HOOK)
    elif shell_name == "fish":
        config.hook_fish_path.parent.mkdir(parents=True, exist_ok=True)
        config.hook_fish_path.write_text(FISH_HOOK)


def _install_git_hooks(config) -> None:
    """Copy git hooks and set core.hooksPath globally."""
    hooks_src = Path(__file__).parent.parent / "git-hooks"
    hooks_dst = config.git_hooks_dir
    hooks_dst.mkdir(parents=True, exist_ok=True)

    if hooks_src.exists():
        for name in ("post-commit", "post-checkout", "pre-push", "post-merge"):
            src = hooks_src / name
            dst = hooks_dst / name
            if src.exists():
                shutil.copy2(str(src), str(dst))
                os.chmod(str(dst), 0o755)
    else:
        # Installed via pip — hooks are bundled in devmem._hooks
        from devmem._hooks import GIT_POST_COMMIT, GIT_POST_CHECKOUT, GIT_PRE_PUSH, GIT_POST_MERGE
        for name, content in [
            ("post-commit", GIT_POST_COMMIT),
            ("post-checkout", GIT_POST_CHECKOUT),
            ("pre-push", GIT_PRE_PUSH),
            ("post-merge", GIT_POST_MERGE),
        ]:
            dst = hooks_dst / name
            dst.write_text(content)
            os.chmod(str(dst), 0o755)

    try:
        subprocess.run(
            ["git", "config", "--global", "core.hooksPath", str(hooks_dst)],
            check=True,
            capture_output=True,
        )
        console.print(f"  → [green]✓[/green] Set core.hooksPath = {hooks_dst}")
    except subprocess.CalledProcessError as exc:
        console.print(f"  → [yellow]Warning:[/yellow] could not set git hooksPath: {exc.stderr.decode()}")


def _start_daemon(config) -> None:
    """Try to start via systemd, fallback to background subprocess."""
    from devmem.daemon import write_systemd_unit, is_running

    if is_running(config):
        console.print("  → [dim]Daemon already running[/dim]")
        return

    try:
        unit_path = write_systemd_unit(config)
        subprocess.run(
            ["systemctl", "--user", "enable", "--now", "devmem"],
            check=True,
            capture_output=True,
        )
        console.print(f"  → [green]✓[/green] Installed systemd user service ({unit_path})")
        # Get PID
        time.sleep(1)
        result = subprocess.run(
            ["systemctl", "--user", "show", "devmem", "--property=MainPID"],
            capture_output=True, text=True,
        )
        pid = result.stdout.strip().split("=")[-1]
        console.print(f"  → devmem.service is running (PID {pid})")
    except (subprocess.CalledProcessError, FileNotFoundError):
        # systemd not available — start as background process
        from devmem.daemon import start_daemon_background
        try:
            pid = start_daemon_background(config)
            console.print(f"  → [green]✓[/green] Started daemon in background (PID {pid})")
        except Exception as exc:
            console.print(f"  → [yellow]Warning:[/yellow] could not start daemon: {exc}")
            console.print("    Run manually: [cyan]devmem daemon start[/cyan]")


# ---------------------------------------------------------------------------
# daemon
# ---------------------------------------------------------------------------


@cli.group()
def daemon():
    """Manage the DevMem background daemon."""
    pass


@daemon.command("start")
@click.option("--foreground", is_flag=True, help="Run in foreground (for systemd/debug)")
def daemon_start(foreground: bool):
    """Start the daemon."""
    config = load_config()
    from devmem.daemon import is_running, Daemon

    if not foreground and is_running(config):
        console.print("[yellow]Daemon is already running.[/yellow]")
        return

    if foreground:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)-7s %(name)s — %(message)s",
        )
        d = Daemon(config)
        d.start(foreground=True)
    else:
        from devmem.daemon import start_daemon_background
        pid = start_daemon_background(config)
        time.sleep(1)
        console.print(f"[green]Daemon started (PID {pid})[/green]")


@daemon.command("stop")
def daemon_stop():
    """Stop the daemon."""
    config = load_config()
    from devmem.daemon import stop_daemon, is_running

    if not is_running(config):
        console.print("[yellow]Daemon is not running.[/yellow]")
        return
    if stop_daemon(config):
        console.print("[green]Daemon stopped.[/green]")
    else:
        console.print("[red]Could not stop daemon.[/red]")


@daemon.command("status")
def daemon_status():
    """Show daemon status and stats."""
    config = load_config()
    from devmem.daemon import is_running, read_pid

    running = is_running(config)
    pid = read_pid(config)
    status_str = "[green]running[/green]" if running else "[red]stopped[/red]"
    console.print(f"Status: {status_str}" + (f" (PID {pid})" if pid else ""))

    if config.db_path.exists():
        from devmem.storage.db import DB
        from devmem.storage.vectors import VectorStore

        db = DB(config.db_path)
        total = db.get_event_count()
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        today_events = len(db.get_events_by_date(today))
        db.close()

        vectors = VectorStore.from_file(config.faiss_path, dim=config.embedding_dim)

        console.print(f"Events total:  {total}")
        console.print(f"Events today:  {today_events}")
        console.print(f"Vectors:       {vectors.size()}")
        console.print(f"DB size:       {_human_size(config.db_path.stat().st_size)}")


@daemon.command("install")
def daemon_install():
    """Install and enable the systemd user service."""
    config = load_config()
    from devmem.daemon import write_systemd_unit

    unit_path = write_systemd_unit(config)
    console.print(f"Written: {unit_path}")
    try:
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "--user", "enable", "--now", "devmem"], check=True)
        console.print("[green]Service enabled and started.[/green]")
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        console.print(f"[yellow]systemctl failed: {exc}[/yellow]")
        console.print("Start manually: [cyan]devmem daemon start[/cyan]")


@daemon.command("logs")
@click.option("--lines", "-n", default=50, help="Number of lines to show")
def daemon_logs(lines: int):
    """Tail the daemon log file."""
    config = load_config()
    if not config.log_path.exists():
        console.print("[yellow]No log file found.[/yellow]")
        return
    try:
        result = subprocess.run(
            ["tail", f"-n{lines}", str(config.log_path)],
            capture_output=True, text=True,
        )
        console.print(result.stdout)
    except FileNotFoundError:
        # tail not available, fallback
        lines_all = config.log_path.read_text().splitlines()
        for line in lines_all[-lines:]:
            console.print(line)


# ---------------------------------------------------------------------------
# ask
# ---------------------------------------------------------------------------


@cli.command()
@click.argument("query")
@click.option("--top-k", default=10, help="Number of results to retrieve")
@click.option("--show-events", is_flag=True, help="Print retrieved events alongside the answer")
@click.option("--no-llm", is_flag=True, help="Skip LLM, just show retrieved events")
def ask(query: str, top_k: int, show_events: bool, no_llm: bool):
    """Answer a question about your work history using AI."""
    config = load_config()
    _ensure_db(config)

    from devmem.storage.db import DB
    from devmem.storage.vectors import VectorStore
    from devmem.processor.embedder import EmbedderQueue
    from devmem.query.retriever import Retriever
    from devmem.query.context import build_prompt_ask
    from devmem.query.llm import is_available, ask as llm_ask, DevMemLLMError, configure
    from devmem.query.timeparser import parse_time_expression, humanise_range

    configure(model=config.llm_model)

    db = DB(config.db_path)
    vectors = VectorStore.from_file(config.faiss_path, dim=config.embedding_dim)
    embedder = EmbedderQueue(db=db, vectors=vectors, model_name=config.embedding_model)
    retriever = Retriever(db=db, vectors=vectors, embedder=embedder)

    with console.status("[dim]Searching…[/dim]"):
        parsed_range = parse_time_expression(query)
        events = retriever.search(query, top_k=top_k)

    time_range_str: Optional[str] = None
    if parsed_range:
        time_range_str = humanise_range(parsed_range[0], parsed_range[1])

    if not events:
        console.print("[yellow]No matching events found.[/yellow]")
        db.close()
        return

    if no_llm or not is_available():
        if not no_llm:
            console.print("[dim]No LLM key configured — showing raw events.[/dim]")
        _print_events_table(events)
        db.close()
        return

    try:
        with console.status("[dim]Asking LLM…[/dim]"):
            messages = build_prompt_ask(query, events, time_range_str)
            answer = llm_ask(messages)
    except DevMemLLMError as exc:
        console.print(f"[red]LLM error:[/red] {exc}")
        _print_events_table(events)
        db.close()
        return

    console.print()
    console.print(answer)

    if show_events:
        console.print()
        _print_events_table(events)

    db.close()


# ---------------------------------------------------------------------------
# today
# ---------------------------------------------------------------------------


@cli.command()
@click.option("--raw", is_flag=True, help="Skip LLM, show chronological event list")
def today(raw: bool):
    """Show what you worked on today."""
    config = load_config()
    _ensure_db(config)

    from devmem.storage.db import DB
    from devmem.query.llm import is_available, ask as llm_ask, DevMemLLMError, configure
    from devmem.query.context import build_prompt_summary

    configure(model=config.llm_model)
    db = DB(config.db_path)
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    events = db.get_events_by_date(date_str)

    if not events:
        console.print("[yellow]No events recorded today yet.[/yellow]")
        console.print("[dim]Make sure the daemon is running: devmem daemon status[/dim]")
        db.close()
        return

    if raw:
        _print_events_table(events, title=f"Today ({date_str})")
        db.close()
        return

    # Check for cached summary
    summary_row = db.get_daily_summary(date_str)
    if summary_row and summary_row.get("summary"):
        console.rule(f"[bold]Today — {date_str}[/bold]")
        console.print(summary_row["summary"])
        db.close()
        return

    if not is_available():
        console.print("[dim]No LLM key — showing raw events.[/dim]")
        _print_events_table(events, title=f"Today ({date_str})")
        db.close()
        return

    try:
        with console.status("[dim]Generating summary…[/dim]"):
            messages = build_prompt_summary(date_str, events)
            summary = llm_ask(messages)
        repos = list({e.repo_name for e in events if e.repo_name})
        highlights = [e.content for e in events if "commit" in e.event_type.value][:10]
        db.upsert_daily_summary(date_str, summary, repos, highlights, len(events))
        console.rule(f"[bold]Today — {date_str}[/bold]")
        console.print(summary)
    except DevMemLLMError as exc:
        console.print(f"[red]LLM error:[/red] {exc}")
        _print_events_table(events, title=f"Today ({date_str})")

    db.close()


# ---------------------------------------------------------------------------
# week
# ---------------------------------------------------------------------------


@cli.command()
@click.option("--raw", is_flag=True, help="Skip LLM, show per-day event list")
def week(raw: bool):
    """Show what you worked on this week."""
    config = load_config()
    _ensure_db(config)

    from devmem.storage.db import DB
    from devmem.query.llm import is_available, ask as llm_ask, DevMemLLMError, configure
    from devmem.query.context import build_prompt_summary

    configure(model=config.llm_model)
    db = DB(config.db_path)

    now = datetime.now(timezone.utc)
    monday = now.replace(hour=0, minute=0, second=0, microsecond=0)
    monday -= __import__("datetime").timedelta(days=now.weekday())
    start_str = monday.strftime("%Y-%m-%dT%H:%M:%SZ")
    end_str = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    events = db.get_events_by_date_range(start_str, end_str)

    if not events:
        console.print("[yellow]No events recorded this week yet.[/yellow]")
        db.close()
        return

    if raw:
        # Group by date
        by_date: dict[str, list] = {}
        for e in events:
            by_date.setdefault(e.date, []).append(e)
        for date, day_events in sorted(by_date.items()):
            _print_events_table(day_events, title=date)
        db.close()
        return

    if not is_available():
        console.print("[dim]No LLM key — showing raw events.[/dim]")
        _print_events_table(events, title="This Week")
        db.close()
        return

    week_label = f"{monday.strftime('%b %-d')} – {now.strftime('%b %-d')}"
    try:
        with console.status("[dim]Generating weekly summary…[/dim]"):
            messages = build_prompt_summary(week_label, events)
            summary = llm_ask(messages)
        console.rule(f"[bold]This Week — {week_label}[/bold]")
        console.print(summary)
    except DevMemLLMError as exc:
        console.print(f"[red]LLM error:[/red] {exc}")
        _print_events_table(events, title="This Week")

    db.close()


# ---------------------------------------------------------------------------
# timeline
# ---------------------------------------------------------------------------


@cli.command()
@click.option("--date", default=None, help="Date to show (YYYY-MM-DD, default: today)")
@click.option("--repo", default=None, help="Filter by repo name")
def timeline(date: Optional[str], repo: Optional[str]):
    """Chronological activity timeline, grouped by session."""
    config = load_config()
    _ensure_db(config)

    from devmem.storage.db import DB

    db = DB(config.db_path)
    date_str = date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    events = db.get_events_by_date(date_str)

    if repo:
        events = [e for e in events if e.repo_name == repo]

    if not events:
        console.print(f"[yellow]No events for {date_str}.[/yellow]")
        db.close()
        return

    console.rule(f"[bold]Timeline — {date_str}[/bold]")

    # Group by session
    sessions: dict[str, list] = {}
    for e in events:
        sid = e.session_id or "unsessioned"
        sessions.setdefault(sid, []).append(e)

    for sid, sess_events in sessions.items():
        first_ts = sess_events[0].timestamp
        last_ts = sess_events[-1].timestamp
        repo_names = list({e.repo_name for e in sess_events if e.repo_name})
        repo_label = ", ".join(repo_names) if repo_names else "unknown"
        console.print(
            f"\n[dim]Session {sid[:8]} · {_fmt_ts(first_ts)}–{_fmt_ts(last_ts)} · {repo_label}[/dim]"
        )
        for event in sess_events:
            icon = _ICONS.get(event.event_type.value, "·")
            style = _TYPE_STYLES.get(event.event_type.value, "white")
            ts = _fmt_ts(event.timestamp)
            console.print(f"  [dim]{ts}[/dim] [{style}]{icon}[/{style}] {event.content}")

    db.close()


# ---------------------------------------------------------------------------
# repos
# ---------------------------------------------------------------------------


@cli.command()
@click.option("--sort", type=click.Choice(["activity", "name", "count"]), default="activity")
def repos(sort: str):
    """List all tracked repositories."""
    config = load_config()
    _ensure_db(config)

    from devmem.storage.db import DB

    db = DB(config.db_path)
    all_repos = db.get_all_repos()
    db.close()

    if not all_repos:
        console.print("[yellow]No repos tracked yet.[/yellow]")
        return

    if sort == "name":
        all_repos.sort(key=lambda r: r["name"].lower())
    elif sort == "count":
        all_repos.sort(key=lambda r: r["event_count"], reverse=True)
    # default "activity" is already sorted by last_active DESC from DB

    table = Table(title="Tracked Repositories", box=box.ROUNDED)
    table.add_column("Repo", style="bold")
    table.add_column("Last Active")
    table.add_column("Events", justify="right")
    table.add_column("Path", style="dim")

    for r in all_repos:
        last_active = r.get("last_active") or r.get("first_seen", "?")
        try:
            dt = datetime.fromisoformat(last_active.replace("Z", "+00:00"))
            last_str = dt.strftime("%Y-%m-%d %H:%M")
        except (ValueError, AttributeError):
            last_str = last_active
        table.add_row(r["name"], last_str, str(r["event_count"]), r["path"])

    console.print(table)


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


@cli.command()
@click.argument("query")
@click.option("--type", "event_type", default=None, help="Filter by event type")
@click.option("--repo", default=None, help="Filter by repo name")
@click.option("--since", default=None, help="Start date (YYYY-MM-DD)")
@click.option("--until", default=None, help="End date (YYYY-MM-DD)")
@click.option("--top-k", default=20, help="Max results to return")
def search(query: str, event_type: Optional[str], repo: Optional[str],
           since: Optional[str], until: Optional[str], top_k: int):
    """Search activity history without LLM."""
    config = load_config()
    _ensure_db(config)

    from devmem.storage.db import DB
    from devmem.storage.vectors import VectorStore
    from devmem.processor.embedder import EmbedderQueue
    from devmem.query.retriever import Retriever

    db = DB(config.db_path)
    vectors = VectorStore.from_file(config.faiss_path, dim=config.embedding_dim)
    embedder = EmbedderQueue(db=db, vectors=vectors, model_name=config.embedding_model)
    retriever = Retriever(db=db, vectors=vectors, embedder=embedder)

    date_range = None
    if since or until:
        start_dt = datetime.fromisoformat((since or "2000-01-01") + "T00:00:00+00:00")
        end_dt = datetime.fromisoformat((until or "2099-12-31") + "T23:59:59+00:00")
        date_range = (start_dt, end_dt)

    etypes = None
    if event_type:
        try:
            etypes = [EventType(event_type)]
        except ValueError:
            console.print(f"[red]Unknown event type: {event_type}[/red]")
            console.print(f"Valid types: {', '.join(e.value for e in EventType)}")
            db.close()
            return

    events = retriever.search(query, top_k=top_k, date_range=date_range,
                              event_types=etypes, repo_name=repo)

    if not events:
        console.print("[yellow]No results found.[/yellow]")
        db.close()
        return

    _print_events_table(events, title=f"Search: {query}")
    db.close()


# ---------------------------------------------------------------------------
# stats
# ---------------------------------------------------------------------------


@cli.command()
def stats():
    """Show capture statistics."""
    config = load_config()
    _ensure_db(config)

    from devmem.storage.db import DB
    from devmem.storage.vectors import VectorStore
    from devmem.daemon import is_running

    db = DB(config.db_path)
    vectors = VectorStore.from_file(config.faiss_path, dim=config.embedding_dim)

    # Events by type
    counts = db.get_event_counts_by_type()
    type_table = Table(title="Events by Type", box=box.ROUNDED)
    type_table.add_column("Type")
    type_table.add_column("Count", justify="right")
    for et in EventType:
        type_table.add_row(et.value, str(counts.get(et.value, 0)))
    console.print(type_table)

    # Events per day (last 7)
    per_day = db.get_events_per_day(7)
    day_table = Table(title="Last 7 Days", box=box.ROUNDED)
    day_table.add_column("Date")
    day_table.add_column("Events", justify="right")
    day_table.add_column("Sparkline")
    max_count = max((r["cnt"] for r in per_day), default=1)
    for row in reversed(per_day):
        bar = "█" * int(row["cnt"] / max_count * 10)
        day_table.add_row(row["date"], str(row["cnt"]), f"[cyan]{bar}[/cyan]")
    console.print(day_table)

    # Most active repos
    all_repos = db.get_all_repos()[:10]
    repo_table = Table(title="Most Active Repos", box=box.ROUNDED)
    repo_table.add_column("Repo")
    repo_table.add_column("Events", justify="right")
    for r in sorted(all_repos, key=lambda x: x["event_count"], reverse=True)[:5]:
        repo_table.add_row(r["name"], str(r["event_count"]))
    console.print(repo_table)

    # Misc stats
    db_size = config.db_path.stat().st_size if config.db_path.exists() else 0
    console.print(f"Total events:  {db.get_event_count()}")
    console.print(f"Vector index:  {vectors.size()} vectors")
    console.print(f"DB size:       {_human_size(db_size)}")
    console.print(f"Daemon:        {'[green]running[/green]' if is_running(config) else '[red]stopped[/red]'}")

    db.close()


# ---------------------------------------------------------------------------
# privacy
# ---------------------------------------------------------------------------


@cli.group()
def privacy():
    """Manage privacy settings and delete captured data."""
    pass


@privacy.command("list")
def privacy_list():
    """Show what is being captured and counts per event type."""
    config = load_config()
    _ensure_db(config)

    from devmem.storage.db import DB

    db = DB(config.db_path)
    counts = db.get_event_counts_by_type()
    db.close()

    capture = config.capture
    priv = config.privacy

    console.rule("[bold]Capture Settings[/bold]")
    for key, enabled in capture.items():
        status = "[green]ON[/green]" if enabled else "[red]OFF[/red]"
        count = counts.get(key, 0)
        console.print(f"  {key:<20} {status}  ({count} events)")

    console.print()
    console.rule("[bold]Privacy Filters[/bold]")
    console.print("Command ignore patterns:")
    for p in priv.get("cmd_ignore_patterns", []):
        console.print(f"  [dim]•[/dim] {p}")
    console.print("File ignore patterns:")
    for p in priv.get("file_ignore_patterns", []):
        console.print(f"  [dim]•[/dim] {p}")


@privacy.command("delete")
@click.option("--before", default=None, help="Delete events before this date (YYYY-MM-DD)")
@click.option("--type", "event_type", default=None, help="Delete all events of this type")
@click.confirmation_option(prompt="This will permanently delete events. Continue?")
def privacy_delete(before: Optional[str], event_type: Optional[str]):
    """Delete captured events."""
    config = load_config()
    _ensure_db(config)

    from devmem.storage.db import DB

    db = DB(config.db_path)

    if before:
        n = db.delete_events_before(before)
        console.print(f"[green]Deleted {n} events before {before}.[/green]")

    if event_type:
        # Direct SQL delete by type
        with db._tx() as conn:
            cur = conn.execute("DELETE FROM events WHERE event_type = ?", (event_type,))
            n = cur.rowcount
        console.print(f"[green]Deleted {n} events of type '{event_type}'.[/green]")

    db.close()


@privacy.command("ignore")
@click.option("--cmd", default=None, help="Add a command pattern to ignore")
def privacy_ignore(cmd: Optional[str]):
    """Add an ignore pattern to the privacy config."""
    config = load_config()

    if cmd:
        patterns = config.privacy.get("cmd_ignore_patterns", [])
        if cmd not in patterns:
            patterns.append(cmd)
            config._data["privacy"]["cmd_ignore_patterns"] = patterns
            save_config(config)
            console.print(f"[green]Added ignore pattern: {cmd}[/green]")
        else:
            console.print(f"[yellow]Pattern already exists: {cmd}[/yellow]")


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------


@cli.command("config")
@click.argument("key", required=False)
@click.argument("value", required=False)
def config_cmd(key: Optional[str], value: Optional[str]):
    """Get or set configuration values.

    \b
    Examples:
      devmem config                    # show all
      devmem config daemon_port        # show one value
      devmem config daemon_port 8080   # set a value
    """
    config = load_config()

    if key is None:
        console.print_json(json.dumps(config.as_dict(), indent=2))
        return

    if value is None:
        val = config.get(key)
        if val is None:
            console.print(f"[yellow]Key not found: {key}[/yellow]")
        else:
            console.print(f"{key} = {val!r}")
        return

    config.set(key, value)
    save_config(config)
    console.print(f"[green]{key} = {config.get(key)!r}[/green]")


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------


@cli.command()
@click.option("--from", "from_date", default=None, help="Start date YYYY-MM-DD")
@click.option("--to", "to_date", default=None, help="End date YYYY-MM-DD")
@click.option("--type", "event_type", default=None, help="Filter by event type")
@click.option("--format", "fmt", type=click.Choice(["json", "csv"]), default="json")
@click.option("--output", "-o", default=None, help="Output file (default: stdout)")
def export(from_date: Optional[str], to_date: Optional[str],
           event_type: Optional[str], fmt: str, output: Optional[str]):
    """Export events as JSON or CSV."""
    config = load_config()
    _ensure_db(config)

    from devmem.storage.db import DB

    db = DB(config.db_path)
    etypes = [EventType(event_type)] if event_type else None
    date_range = None
    if from_date or to_date:
        date_range = (
            (from_date or "2000-01-01") + "T00:00:00Z",
            (to_date or "2099-12-31") + "T23:59:59Z",
        )

    events = db.get_events_by_filters(date_range=date_range, event_types=etypes, limit=100_000)
    db.close()

    if fmt == "json":
        data = json.dumps([e.to_dict() for e in events], indent=2)
    else:
        import csv
        import io

        buf = io.StringIO()
        writer = csv.DictWriter(
            buf,
            fieldnames=["id", "timestamp", "date", "event_type", "source",
                        "repo_name", "content"],
        )
        writer.writeheader()
        for e in events:
            writer.writerow({
                "id": e.id,
                "timestamp": e.timestamp,
                "date": e.date,
                "event_type": e.event_type.value,
                "source": e.source.value,
                "repo_name": e.repo_name or "",
                "content": e.content,
            })
        data = buf.getvalue()

    if output:
        Path(output).write_text(data)
        console.print(f"[green]Exported {len(events)} events to {output}[/green]")
    else:
        print(data)


# ---------------------------------------------------------------------------
# mcp-serve
# ---------------------------------------------------------------------------


@cli.command("mcp-serve")
def mcp_serve():
    """Start the MCP server on stdio (for Claude Code / Copilot integration)."""
    from devmem.mcp_server import run_mcp_server

    run_mcp_server()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ensure_db(config) -> None:
    if not config.db_path.exists():
        err_console.print(
            "[red]DevMem database not found. Run: devmem init[/red]"
        )
        sys.exit(1)


def _print_events_table(events, title: str = "Results") -> None:
    table = Table(title=title, box=box.ROUNDED, show_lines=False)
    table.add_column("Time", style="dim", min_width=16)
    table.add_column("Type", min_width=10)
    table.add_column("Content")
    table.add_column("Repo", style="dim")

    for event in events:
        icon = _ICONS.get(event.event_type.value, "·")
        style = _TYPE_STYLES.get(event.event_type.value, "white")
        ts = _fmt_ts(event.timestamp)
        type_cell = Text(f"{icon} {event.event_type.value}", style=style)
        table.add_row(ts, type_cell, event.content[:80], event.repo_name or "")

    console.print(table)


def _fmt_ts(ts_str: str) -> str:
    try:
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        return dt.astimezone().strftime("%m-%d %H:%M")
    except ValueError:
        return ts_str[:16]


def _human_size(size_bytes: int) -> str:
    size = float(size_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"
