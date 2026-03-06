# =============================================================================
# MEMORY - Persistent Chat and Tool Log
# =============================================================================
# Stores the complete history of all AI assistant interactions to disk.
#
# Responsibilities:
#   - Append every message, tool call, and tool result with timestamps
#   - Seed new sessions with the last N user/assistant exchanges
#   - Search history by keyword, date, or recency
#   - Provide tool usage statistics
#
# Storage format (memory.json):
# {
#   "chat_id_123": [
#     {"role": "user",        "content": "...",  "timestamp": "2026-02-24T14:55:00"},
#     {"role": "assistant",   "content": "...",  "timestamp": "2026-02-24T14:55:02"},
#     {"role": "tool_call",   "name": "get_temperatures", "input": {}, "timestamp": "..."},
#     {"role": "tool_result", "name": "get_temperatures", "content": {...}, "timestamp": "..."},
#   ]
# }
# =============================================================================

import json
import logging
import os
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

_MEMORY_FILE = os.path.join(os.path.dirname(__file__), "memory.json")


# =============================================================================
# SECTION 1: DISK I/O
# =============================================================================

def _load() -> dict:
    """Load full memory from disk. Returns empty dict if file doesn't exist."""
    if not os.path.exists(_MEMORY_FILE):
        return {}
    try:
        with open(_MEMORY_FILE, "r") as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"⚠️ Could not load memory: {e}")
        return {}


def _save(data: dict) -> None:
    """Write memory to disk atomically."""
    try:
        tmp = _MEMORY_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2, default=str)
        os.replace(tmp, _MEMORY_FILE)
    except Exception as e:
        logger.warning(f"⚠️ Could not save memory: {e}")


def _now() -> str:
    """Return current timestamp as ISO string."""
    return datetime.now().isoformat(timespec="seconds")


def _parse_timestamp(ts: str) -> datetime | None:
    """Parse an ISO timestamp string, returning None on failure."""
    try:
        return datetime.fromisoformat(ts)
    except Exception:
        return None


# =============================================================================
# SECTION 2: WRITE METHODS
# =============================================================================

def append_message(chat_id: int, role: str, content: str) -> None:
    """Append a user or assistant message to the log."""
    data = _load()
    key = str(chat_id)
    if key not in data:
        data[key] = []
    data[key].append({
        "role":      role,
        "content":   content,
        "timestamp": _now(),
    })
    _save(data)


def append_tool_call(chat_id: int, tool_id: str, name: str, input: dict) -> None:
    """Append a tool call request to the log."""
    data = _load()
    key = str(chat_id)
    if key not in data:
        data[key] = []
    data[key].append({
        "role":      "tool_call",
        "tool_id":   tool_id,
        "name":      name,
        "input":     input,
        "timestamp": _now(),
    })
    _save(data)


def append_tool_result(chat_id: int, tool_id: str, name: str, content: dict) -> None:
    """Append a tool result to the log."""
    data = _load()
    key = str(chat_id)
    if key not in data:
        data[key] = []
    data[key].append({
        "role":      "tool_result",
        "tool_id":   tool_id,
        "name":      name,
        "content":   content,
        "timestamp": _now(),
    })
    _save(data)


# =============================================================================
# SECTION 3: READ METHODS
# =============================================================================

def get_recent(chat_id: int, n: int) -> list[dict]:
    """
    Return the last N user/assistant exchanges for seeding a new session.
    Filters out tool_call and tool_result entries.
    """
    if n == 0:
        return []

    data = _load()
    key = str(chat_id)
    all_entries = data.get(key, [])

    messages = [
        {"role": e["role"], "content": e["content"]}
        for e in all_entries
        if e["role"] in ("user", "assistant")
    ]

    return messages[-n:] if len(messages) > n else messages


def get_last_n(chat_id: int, n: int, exclude_seconds: int = 5) -> list[dict]:
    """
    Return the last N user/assistant exchanges with timestamps.

    Args:
        chat_id:         Telegram chat ID
        n:               Number of exchanges to return
        exclude_seconds: Exclude entries written within this many seconds
                         (prevents returning the message that triggered the call)

    Returns:
        List of {"role", "content", "timestamp"} dicts, oldest first.
    """
    data = _load()
    key = str(chat_id)
    all_entries = data.get(key, [])

    cutoff = datetime.now() - timedelta(seconds=exclude_seconds)

    messages = []
    for e in all_entries:
        if e["role"] not in ("user", "assistant"):
            continue
        ts = _parse_timestamp(e.get("timestamp", ""))
        if ts and ts > cutoff:
            continue  # Skip very recent entries to avoid self-reference
        messages.append({
            "role":      e["role"],
            "content":   e["content"],
            "timestamp": e.get("timestamp", "unknown"),
        })

    return messages[-n:] if len(messages) > n else messages


def search(
    chat_id: int,
    query: str,
    max_results: int = 5,
    since: datetime | None = None,
    until: datetime | None = None,
    exclude_seconds: int = 5,
) -> list[dict]:
    """
    Search message history by keyword and optional date range.

    Args:
        chat_id:         Telegram chat ID
        query:           Keyword or phrase. Empty string matches all messages.
        max_results:     Maximum results to return
        since:           Only return messages after this datetime
        until:           Only return messages before this datetime
        exclude_seconds: Exclude entries written within this many seconds
                         (prevents the triggering message appearing in results)

    Returns:
        List of matching entries with timestamps, newest first.
    """
    data = _load()
    key = str(chat_id)
    all_entries = data.get(key, [])

    query_lower = query.lower().strip()
    cutoff = datetime.now() - timedelta(seconds=exclude_seconds)

    # Split into individual keywords for multi-keyword matching
    keywords = [k.strip() for k in query_lower.split(",") if k.strip()]

    matches = []
    for e in all_entries:
        if e["role"] not in ("user", "assistant"):
            continue

        ts = _parse_timestamp(e.get("timestamp", ""))

        # Exclude very recent entries
        if ts and ts > cutoff:
            continue

        # Date range filters
        if since and ts and ts < since:
            continue
        if until and ts and ts > until:
            continue

        # Keyword filter — match if any keyword found (empty = match all)
        content = e.get("content", "").lower()
        if keywords and not any(kw in content for kw in keywords):
            continue

        matches.append({
            "role":      e["role"],
            "content":   content,
            "timestamp": e.get("timestamp", "unknown"),
        })

    # Newest first, limited to max_results
    return list(reversed(matches))[:max_results]


def get_tool_stats(chat_id: int) -> dict:
    """
    Return tool usage statistics for a chat_id.

    Returns:
        Dict with total call count and per-tool breakdown.
    """
    data = _load()
    key = str(chat_id)
    all_entries = data.get(key, [])

    counts: dict[str, int] = {}
    for entry in all_entries:
        if entry["role"] == "tool_call":
            name = entry.get("name", "unknown")
            counts[name] = counts.get(name, 0) + 1

    return {
        "total_tool_calls": sum(counts.values()),
        "by_tool": counts,
    }


# =============================================================================
# SECTION 4: DATE PARSING HELPERS
# =============================================================================

def parse_date_query(query: str) -> tuple[datetime | None, datetime | None]:
    """
    Parse natural language date expressions into (since, until) datetime pairs.

    Supports:
        "today", "yesterday"
        "last week", "this week"
        "last N days"
        "YYYY-MM-DD" exact date

    Returns:
        (since, until) tuple — either may be None if not determinable.
    """
    now = datetime.now()
    q = query.lower().strip()

    if q == "today":
        since = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return since, None

    if q == "yesterday":
        yesterday = now - timedelta(days=1)
        since = yesterday.replace(hour=0, minute=0, second=0, microsecond=0)
        until = yesterday.replace(hour=23, minute=59, second=59)
        return since, until

    if q in ("last week", "this week"):
        since = now - timedelta(days=7)
        return since, None

    if q.startswith("last ") and q.endswith(" days"):
        try:
            n = int(q.split()[1])
            since = now - timedelta(days=n)
            return since, None
        except (ValueError, IndexError):
            pass

    # Try exact date YYYY-MM-DD
    try:
        date = datetime.strptime(q, "%Y-%m-%d")
        since = date.replace(hour=0, minute=0, second=0)
        until = date.replace(hour=23, minute=59, second=59)
        return since, until
    except ValueError:
        pass

    return None, None