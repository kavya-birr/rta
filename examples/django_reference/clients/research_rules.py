"""Research-rules store — the editable thresholds that drive the rule-based
AI Insights engine in clients/research.py.

Each rule has:
  * key       — programmatic name used in code
  * label     — human-readable name shown in the UI
  * unit      — % / ₹ / ppt — drives input formatting
  * default   — out-of-the-box threshold
  * min/max   — guardrails for the editor
  * step      — input step (1, 0.5, 1000, etc.)
  * group     — which insight rule it belongs to
  * help      — short explanation shown in the editor
  * severity_high  — optional: severity escalates above this value
  * severity_low   — optional: severity de-escalates below this value

We persist user overrides to ``research_rules.json`` next to the project
(JSON-file pattern, no migration). Fetching always falls back to defaults
when a key is missing.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from django.conf import settings


# ─────────────────────────────────────────────────────────────────────
# Rule catalogue — single source of truth for all thresholds
# ─────────────────────────────────────────────────────────────────────

RULE_DEFS: list[dict[str, Any]] = [
    # ── Concentration risk ──
    {
        "key": "concentration_pct",
        "group": "Concentration",
        "label": "Top-scheme concentration threshold",
        "unit": "%",
        "default": 40, "min": 10, "max": 95, "step": 5,
        "severity_high": 60,
        "help": "Flag any client whose single largest holding exceeds this percentage of their total AUM. Severity escalates to 'high' above the high threshold.",
    },
    {
        "key": "concentration_severity_high_pct",
        "group": "Concentration",
        "label": "High-severity concentration cutoff",
        "unit": "%",
        "default": 60, "min": 30, "max": 95, "step": 5,
        "help": "Above this concentration % the insight is shown as 'high' severity instead of 'medium'.",
    },

    # ── Missing asset class ──
    {
        "key": "missing_debt_min_aum",
        "group": "Missing Asset Class",
        "label": "Minimum AUM to flag missing debt",
        "unit": "₹",
        "default": 100000, "min": 10000, "max": 5000000, "step": 10000,
        "help": "Only flag '100% equity, no debt allocation' for clients with at least this much AUM. Smaller portfolios can reasonably stay in pure equity.",
    },

    # ── Underperformer ──
    {
        "key": "underperformer_xirr_pct",
        "group": "Underperformer",
        "label": "Underperformer XIRR threshold",
        "unit": "%",
        "default": -5, "min": -50, "max": 0, "step": 1,
        "help": "Flag any holding whose XIRR is below this value (negative numbers OK). E.g., -5% means flag holdings losing more than 5% per year on an annualised basis.",
    },

    # ── Tax-loss ──
    {
        "key": "tax_loss_min",
        "group": "Tax-Loss Harvesting",
        "label": "Minimum unrealised loss to flag",
        "unit": "₹",
        "default": 25000, "min": 1000, "max": 1000000, "step": 1000,
        "help": "Highlight tax-loss harvesting candidates only when the unrealised loss exceeds this rupee amount. Smaller losses aren't worth the transaction friction.",
    },

    # ── Book-level skew ──
    {
        "key": "book_equity_skew_pct",
        "group": "Book-Level Skew",
        "label": "Book equity skew threshold",
        "unit": "%",
        "default": 75, "min": 50, "max": 100, "step": 5,
        "help": "Raise a book-wide alert if the entire ARN's equity allocation exceeds this percentage. High concentration → systemic drawdown risk.",
    },
    {
        "key": "book_debt_floor_pct",
        "group": "Book-Level Skew",
        "label": "Book debt floor threshold",
        "unit": "%",
        "default": 10, "min": 0, "max": 50, "step": 1,
        "help": "Raise a book-wide alert when debt allocation falls below this percentage and the book is large enough to matter.",
    },
    {
        "key": "book_skew_min_aum",
        "group": "Book-Level Skew",
        "label": "Minimum book AUM for skew alerts",
        "unit": "₹",
        "default": 1000000, "min": 100000, "max": 100000000, "step": 100000,
        "help": "Skip book-wide skew warnings for very small books — there isn't enough AUM for the imbalance to matter.",
    },

    # ── Lag (yield-optimizer-style category gap) ──
    {
        "key": "category_lag_ppts",
        "group": "Category Lag",
        "label": "Category-leader lag threshold (ppts)",
        "unit": "ppt",
        "default": 4, "min": 1, "max": 20, "step": 1,
        "help": "Flag any holding whose return lags the best-performing scheme in the same category by this many percentage points. Used by both Yield Optimizer and the per-client laggard insight.",
    },
]

RULE_KEYS = [r["key"] for r in RULE_DEFS]
RULE_BY_KEY = {r["key"]: r for r in RULE_DEFS}


def _store_path() -> Path:
    base = Path(getattr(settings, "DATA_DIR", getattr(settings, "BASE_DIR", ".")))
    return base / "research_rules.json"


def load_overrides() -> dict[str, float]:
    """Return saved overrides only (no defaults). May return {}."""
    p = _store_path()
    if not p.exists():
        return {}
    try:
        with p.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            # Coerce known keys to numeric, drop anything unknown
            out = {}
            for k, v in data.items():
                if k in RULE_BY_KEY:
                    try:
                        out[k] = float(v)
                    except (TypeError, ValueError):
                        pass
            return out
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def save_overrides(overrides: dict[str, Any]) -> None:
    p = _store_path()
    # Filter to known keys, coerce to numeric
    cleaned: dict[str, float] = {}
    for k, v in overrides.items():
        if k not in RULE_BY_KEY:
            continue
        try:
            cleaned[k] = float(v)
        except (TypeError, ValueError):
            continue
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8") as f:
        json.dump(cleaned, f, indent=2)


def reset_to_defaults() -> None:
    p = _store_path()
    if p.exists():
        p.unlink()


def effective_rules() -> dict[str, float]:
    """Defaults merged with user overrides — what the engine actually uses."""
    out = {r["key"]: float(r["default"]) for r in RULE_DEFS}
    out.update(load_overrides())
    return out


def is_overridden(key: str) -> bool:
    return key in load_overrides()


def grouped_for_editor() -> list[dict[str, Any]]:
    """Group rule defs by 'group' for sectioned display in the editor."""
    overrides = load_overrides()
    groups: dict[str, list[dict[str, Any]]] = {}
    for r in RULE_DEFS:
        rule = dict(r)
        rule["current"] = float(overrides.get(r["key"], r["default"]))
        rule["overridden"] = r["key"] in overrides
        groups.setdefault(r["group"], []).append(rule)
    return [{"group": g, "rules": rs} for g, rs in groups.items()]
