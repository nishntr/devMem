"""Prompt assembly for `ask` and daily summary queries."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import humanize

from devmem.models import Event


# ---------------------------------------------------------------------------
# System prompts
# ---------------------------------------------------------------------------

_ASK_SYSTEM = """\
You are a developer's personal memory assistant.
Answer questions about their work history based on the activity log provided.
Be specific: mention exact commands, file names, repo names, dates.
If the context doesn't contain enough information, say so clearly.
Keep your answer concise and focused on what was asked."""

_SUMMARY_SYSTEM = """\
You are a developer activity summariser.
Given a list of developer activities for a day, produce a concise daily summary.
Structure the summary as:
1. Repos worked on (with brief description of what was done)
2. Key accomplishments (commits, notable commands, problem-solving)
3. A one-sentence overall summary of the day

Be specific and mention exact repo names, commit messages, and file names where relevant."""


# ---------------------------------------------------------------------------
# Prompt builders
# ---------------------------------------------------------------------------


def build_prompt_ask(
    query: str,
    events: list[Event],
    time_range: Optional[str] = None,
) -> list[dict]:
    """
    Build messages for `devmem ask "<query>"`.

    Parameters
    ----------
    query:
        The user's question.
    events:
        Retrieved events to provide as context.
    time_range:
        Human-readable time range string for display (e.g. "last week").
    """
    now = datetime.now(timezone.utc)

    context_lines: list[str] = []
    for i, event in enumerate(events, 1):
        ts = _humanize_time(event.timestamp, now)
        context_lines.append(f"{i}. [{ts}] {event.content}")

    context = "\n".join(context_lines) if context_lines else "(no matching activity found)"
    time_note = f"\nTime filter: {time_range}" if time_range else ""

    user_content = f"""Activity log:{time_note}

{context}

Question: {query}"""

    return [
        {"role": "system", "content": _ASK_SYSTEM},
        {"role": "user", "content": user_content},
    ]


def build_prompt_summary(date: str, events: list[Event]) -> list[dict]:
    """
    Build messages for the daily summary generator.

    Parameters
    ----------
    date:
        The date being summarised (YYYY-MM-DD).
    events:
        All events for that date.
    """
    now = datetime.now(timezone.utc)

    # Group events by session and repo
    by_type: dict[str, list[str]] = {}
    for event in events:
        et = event.event_type.value
        by_type.setdefault(et, []).append(event.content)

    lines: list[str] = [f"Date: {date}", f"Total activities: {len(events)}", ""]

    for et, contents in by_type.items():
        lines.append(f"== {et.upper().replace('_', ' ')} ({len(contents)} events) ==")
        for c in contents[:20]:  # cap per-type to avoid token bloat
            lines.append(f"  • {c}")
        if len(contents) > 20:
            lines.append(f"  … and {len(contents) - 20} more")
        lines.append("")

    user_content = "\n".join(lines)

    return [
        {"role": "system", "content": _SUMMARY_SYSTEM},
        {"role": "user", "content": user_content},
    ]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _humanize_time(ts_str: str, now: datetime) -> str:
    """Return a human-readable relative time string."""
    try:
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        delta = now - dt.astimezone(timezone.utc)
        if delta.total_seconds() < 0:
            return dt.strftime("%b %-d at %H:%M")
        return humanize.naturaltime(delta)
    except (ValueError, AttributeError):
        return ts_str
