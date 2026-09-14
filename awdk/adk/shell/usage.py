"""
AitherShell Usage Tracking
==========================

Tracks token usage, query count, and estimated cost.
Stored in ~/.aither/usage.json. No external deps.
"""

import json
from datetime import datetime, date
from pathlib import Path
from typing import Dict, Any

USAGE_FILE = Path.home() / ".aither" / "assistant" / "usage.json"


def _load() -> dict:
    if USAGE_FILE.exists():
        with open(USAGE_FILE, "r") as f:
            return json.load(f)
    return {"queries": [], "daily": {}}


def _save(data: dict):
    USAGE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(USAGE_FILE, "w") as f:
        json.dump(data, f, indent=2, default=str)


def record_query(
    model: str = "",
    tokens: int = 0,
    elapsed_ms: int = 0,
    effort: int = 0,
):
    """Record a query for usage tracking."""
    data = _load()
    today = date.today().isoformat()

    # Update daily totals
    if today not in data["daily"]:
        data["daily"][today] = {"queries": 0, "tokens": 0, "elapsed_ms": 0}
    data["daily"][today]["queries"] += 1
    data["daily"][today]["tokens"] += tokens
    data["daily"][today]["elapsed_ms"] += elapsed_ms

    # Keep last 100 individual queries for detail
    data["queries"].append({
        "ts": datetime.now().isoformat(),
        "model": model,
        "tokens": tokens,
        "elapsed_ms": elapsed_ms,
        "effort": effort,
    })
    data["queries"] = data["queries"][-100:]

    _save(data)


def get_usage_summary() -> Dict[str, Any]:
    """Get usage summary."""
    data = _load()
    today = date.today().isoformat()
    daily = data.get("daily", {})
    today_stats = daily.get(today, {"queries": 0, "tokens": 0})

    # Last 7 days
    week_queries = 0
    week_tokens = 0
    for i in range(7):
        d = (date.today() - __import__("datetime").timedelta(days=i)).isoformat()
        if d in daily:
            week_queries += daily[d]["queries"]
            week_tokens += daily[d]["tokens"]

    # All time
    total_queries = sum(d["queries"] for d in daily.values())
    total_tokens = sum(d["tokens"] for d in daily.values())

    # Cost estimate (rough: $0.01 per 1K tokens for local, $0.03 for cloud)
    est_cost_local = total_tokens / 1000 * 0.002  # electricity/amortization
    est_cost_cloud = total_tokens / 1000 * 0.03

    return {
        "today": {
            "queries": today_stats["queries"],
            "tokens": today_stats["tokens"],
        },
        "week": {
            "queries": week_queries,
            "tokens": week_tokens,
        },
        "total": {
            "queries": total_queries,
            "tokens": total_tokens,
            "est_cost_local": f"${est_cost_local:.2f}",
            "est_cost_cloud": f"${est_cost_cloud:.2f}",
        },
        "recent": data.get("queries", [])[-5:],
    }


def get_audit_log(limit: int = 20) -> list:
    """Get recent query audit log."""
    data = _load()
    return data.get("queries", [])[-limit:]
