"""MCP server — exposes Recall tools via the Model Context Protocol (stdio)."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP

logger = logging.getLogger(__name__)

mcp = FastMCP("dev-recall")


# ---------------------------------------------------------------------------
# Tool: recall
# ---------------------------------------------------------------------------


@mcp.tool()
def recall(query: str, days: int = 7, top_k: int = 10) -> str:
    """
    Search developer activity history by natural language query.

    Returns relevant events: terminal commands, git commits, file edits, AI chats.

    Args:
        query: Natural language query about past work
        days: How many days back to search (default 7)
        top_k: Number of results to return (default 10)
    """
    try:
        from recall.config import load_config
        from recall.storage.db import DB
        from recall.storage.vectors import VectorStore
        from recall.processor.embedder import EmbedderQueue
        from recall.query.retriever import Retriever
        from recall.query.timeparser import to_iso

        config = load_config()
        if not config.db_path.exists():
            return "Recall database not found. Run: recall init"

        now = datetime.now(timezone.utc)
        from datetime import timedelta
        start = now - timedelta(days=days)
        date_range = (start, now)

        db = DB(config.db_path)
        vectors = VectorStore.from_file(config.faiss_path, dim=config.embedding_dim)
        embedder = EmbedderQueue(db=db, vectors=vectors, model_name=config.embedding_model)
        retriever = Retriever(db=db, vectors=vectors, embedder=embedder)

        events = retriever.search(query, top_k=top_k, date_range=date_range)
        db.close()

        if not events:
            return f"No activity found matching '{query}' in the last {days} days."

        lines = [f"Found {len(events)} relevant activities:\n"]
        for i, event in enumerate(events, 1):
            ts = _fmt_ts(event.timestamp)
            lines.append(f"{i}. [{ts}] {event.content}")

        return "\n".join(lines)

    except Exception as exc:
        logger.exception("recall tool error")
        return f"Error: {exc}"


# ---------------------------------------------------------------------------
# Tool: today_summary
# ---------------------------------------------------------------------------


@mcp.tool()
def today_summary() -> str:
    """
    Get a summary of what the developer worked on today.
    """
    try:
        from recall.config import load_config
        from recall.storage.db import DB
        from recall.query.llm import is_available, ask as llm_ask, DevMemLLMError, configure
        from recall.query.context import build_prompt_summary

        config = load_config()
        if not config.db_path.exists():
            return "Recall database not found. Run: recall init"

        configure(model=config.llm_model)
        db = DB(config.db_path)
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        events = db.get_events_by_date(date_str)

        if not events:
            db.close()
            return "No activity recorded today yet."

        # Check cached summary
        cached = db.get_daily_summary(date_str)
        if cached and cached.get("summary"):
            db.close()
            return cached["summary"]

        if not is_available():
            db.close()
            lines = [f"Today's activity ({date_str}, {len(events)} events):\n"]
            for e in events[:20]:
                lines.append(f"• {e.content}")
            return "\n".join(lines)

        messages = build_prompt_summary(date_str, events)
        summary = llm_ask(messages)
        db.close()
        return summary

    except Exception as exc:
        logger.exception("today_summary tool error")
        return f"Error: {exc}"


# ---------------------------------------------------------------------------
# Tool: recent_repos
# ---------------------------------------------------------------------------


@mcp.tool()
def recent_repos(days: int = 7) -> str:
    """
    List repositories the developer was active in recently.

    Args:
        days: How many days back to look (default 7)
    """
    try:
        from recall.config import load_config
        from recall.storage.db import DB
        from datetime import timedelta

        config = load_config()
        if not config.db_path.exists():
            return "Recall database not found. Run: recall init"

        db = DB(config.db_path)
        now = datetime.now(timezone.utc)
        start = (now - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        end = now.strftime("%Y-%m-%dT%H:%M:%SZ")

        events = db.get_events_by_date_range(start, end)
        db.close()

        repo_activity: dict[str, int] = {}
        for e in events:
            if e.repo_name:
                repo_activity[e.repo_name] = repo_activity.get(e.repo_name, 0) + 1

        if not repo_activity:
            return f"No repository activity in the last {days} days."

        lines = [f"Active repositories (last {days} days):\n"]
        for repo, count in sorted(repo_activity.items(), key=lambda x: -x[1]):
            lines.append(f"• {repo}  ({count} events)")
        return "\n".join(lines)

    except Exception as exc:
        logger.exception("recent_repos tool error")
        return f"Error: {exc}"


# ---------------------------------------------------------------------------
# Tool: find_command
# ---------------------------------------------------------------------------


@mcp.tool()
def find_command(description: str, repo: Optional[str] = None) -> str:
    """
    Find a terminal command previously run, by describing what it does.

    Args:
        description: Natural language description of what the command does
        repo: Optional: limit search to a specific repo name
    """
    try:
        from recall.config import load_config
        from recall.storage.db import DB
        from recall.storage.vectors import VectorStore
        from recall.processor.embedder import EmbedderQueue
        from recall.query.retriever import Retriever
        from recall.models import EventType

        config = load_config()
        if not config.db_path.exists():
            return "Recall database not found. Run: recall init"

        db = DB(config.db_path)
        vectors = VectorStore.from_file(config.faiss_path, dim=config.embedding_dim)
        embedder = EmbedderQueue(db=db, vectors=vectors, model_name=config.embedding_model)
        retriever = Retriever(db=db, vectors=vectors, embedder=embedder)

        events = retriever.search(
            description,
            top_k=10,
            event_types=[EventType.TERMINAL_CMD],
            repo_name=repo,
        )
        db.close()

        if not events:
            return f"No matching commands found for: '{description}'"

        lines = [f"Commands matching '{description}':\n"]
        for i, event in enumerate(events, 1):
            ts = _fmt_ts(event.timestamp)
            cmd = event.raw_data.get("cmd", event.content)
            lines.append(f"{i}. [{ts}] {cmd}")

        return "\n".join(lines)

    except Exception as exc:
        logger.exception("find_command tool error")
        return f"Error: {exc}"


# ---------------------------------------------------------------------------
# Tool: timeline
# ---------------------------------------------------------------------------


@mcp.tool()
def timeline(date: Optional[str] = None) -> str:
    """
    Get chronological activity log for a specific date.

    Args:
        date: Date in YYYY-MM-DD format. Defaults to today.
    """
    try:
        from recall.config import load_config
        from recall.storage.db import DB

        config = load_config()
        if not config.db_path.exists():
            return "Recall database not found. Run: recall init"

        db = DB(config.db_path)
        date_str = date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        events = db.get_events_by_date(date_str)
        db.close()

        if not events:
            return f"No activity recorded on {date_str}."

        lines = [f"Timeline for {date_str} ({len(events)} events):\n"]
        for event in events:
            ts = _fmt_ts(event.timestamp)
            lines.append(f"[{ts}] {event.event_type.value}: {event.content}")

        return "\n".join(lines)

    except Exception as exc:
        logger.exception("timeline tool error")
        return f"Error: {exc}"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run_mcp_server() -> None:
    """Run the MCP server on stdio."""
    logging.basicConfig(level=logging.WARNING)
    mcp.run(transport="stdio")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fmt_ts(ts_str: str) -> str:
    try:
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        return dt.astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return ts_str[:16]
