"""On-demand insights cache.

When the advisor clicks "Generate insights" for a client, we compute the
rules-engine output once and store it in this JSON cache keyed by PAN.
Subsequent page loads read from the cache instantly — no recomputation
until the advisor explicitly hits "Regenerate".

This keeps the Research page O(1) regardless of book size: even with
10,000 clients the page loads in milliseconds, and the advisor pays the
~1 second per-client cost only when they actually want fresh data.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

from django.conf import settings


def _store_path() -> Path:
    base = Path(getattr(settings, "DATA_DIR", getattr(settings, "BASE_DIR", ".")))
    return base / "client_insights_cache.json"


def _load() -> dict[str, dict[str, Any]]:
    p = _store_path()
    if not p.exists():
        return {}
    try:
        with p.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save(cache: dict[str, dict[str, Any]]) -> None:
    p = _store_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2, default=str)


def get(pan: str) -> dict[str, Any] | None:
    """Return the cached entry for a PAN, or None if never generated."""
    if not pan:
        return None
    return _load().get(pan)


def get_all() -> dict[str, dict[str, Any]]:
    return _load()


def store(pan: str, payload: dict[str, Any]) -> None:
    """Save a freshly-computed insight payload for a client.

    payload must include: insights (list), aum (float). We add generated_at,
    insight_count and max_severity automatically.
    """
    if not pan:
        return
    cache = _load()
    insights = payload.get("insights", [])
    severity_rank = {"high": 0, "medium": 1, "low": 2}
    max_sev = None
    if insights:
        max_sev = sorted(insights, key=lambda x: severity_rank[x["severity"]])[0]["severity"]
    cache[pan] = {
        "insights": insights,
        "aum": float(payload.get("aum", 0)),
        "insight_count": len(insights),
        "max_severity": max_sev,
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "rules_version": payload.get("rules_version"),
    }
    _save(cache)


def invalidate(pan: str) -> None:
    cache = _load()
    if pan in cache:
        cache.pop(pan)
        _save(cache)


def clear_all() -> None:
    _save({})


def stats() -> dict[str, int]:
    """Aggregate counts for the page header."""
    cache = _load()
    sev_counts = {"high": 0, "medium": 0, "low": 0}
    total_insights = 0
    clients_with_insights = 0
    for entry in cache.values():
        if entry.get("insight_count", 0) > 0:
            clients_with_insights += 1
        for ins in entry.get("insights", []):
            sev = ins.get("severity")
            if sev in sev_counts:
                sev_counts[sev] += 1
                total_insights += 1
    return {
        "cached_count": len(cache),
        "clients_with_insights": clients_with_insights,
        "total_insights": total_insights,
        "sev_counts": sev_counts,
    }


def freshness_label(generated_at: str | None) -> str:
    """Human-readable 'computed N ago' string."""
    if not generated_at:
        return "never"
    try:
        when = dt.datetime.fromisoformat(generated_at)
    except ValueError:
        return "unknown"
    delta = dt.datetime.now() - when
    secs = int(delta.total_seconds())
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"
