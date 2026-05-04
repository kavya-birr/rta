"""Yield Optimizer — find low-yield holdings and recommend higher-yield
alternatives in the same SEBI / asset category.

Strategy:
  1. Walk every client holding (across all clients).
  2. Group held schemes by their SEBI category (or asset class fallback).
  3. For each group, compute the AUM-weighted "category leader" (best XIRR;
     fall back to unrealized %).
  4. Flag any holding whose return lags the category leader by ≥ 4 ppts
     AND has at least ₹50,000 invested (small positions are noise).
  5. Compute the "potential uplift" of switching: invested_amount × (lead − held)
     for the next 12 months (rough estimate, illustrative only).

This stays self-contained: comparisons happen inside the existing book —
no external alternative-fund universe is needed. Once the AMFI universe
is wired in, the leader pool can be widened.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Callable

from sqlalchemy import select

from openreversefeed.db.models import Account, Scheme

from .portfolio import (
    classify_asset,
    compute_client_holdings,
    enrich_with_current_value,
)


LAG_THRESHOLD_PPTS = 4.0   # holding return lags leader by this many percentage points
MIN_INVESTED      = 50_000  # ignore positions smaller than this (noise)


@dataclass
class CategorySummary:
    category: str
    holdings_count: int = 0
    aum: float = 0.0
    avg_return_pct: float = 0.0
    leader_scheme: str = ""
    leader_return_pct: float = 0.0


@dataclass
class SwitchSuggestion:
    client_pan: str
    client_name: str
    held_scheme: str
    held_scheme_code: str
    held_isin: str | None
    category: str
    invested: float
    current_value: float
    held_return_pct: float
    leader_scheme: str
    leader_return_pct: float
    return_gap_pct: float       # leader − held
    potential_uplift_1y: float  # invested × gap / 100
    severity: str               # 'low', 'medium', 'high'


def _category_of(holding) -> str:
    """Best available category label for grouping. Falls back to asset class."""
    return (
        getattr(holding, "sebi_category", None)
        or classify_asset(holding.scheme_name)
        or "Other"
    )


def _holding_return_pct(h) -> float | None:
    """Pick the best available return measure for ranking holdings.

    Order of preference:
      1. XIRR (annualised, accounts for cashflow timing)
      2. Total gain % (realized + unrealized over total cost)
      3. Unrealized gain % (when no other data)
    """
    if h.xirr_pct is not None:
        return float(h.xirr_pct)
    if h.total_gain_pct is not None:
        return float(h.total_gain_pct)
    if h.gain_loss_pct is not None:
        return float(h.gain_loss_pct)
    return None


def compute_yield_analysis(
    session, nav_lookup: Callable | None = None
) -> dict[str, Any]:
    """Build the full yield-optimization report.

    Returns:
        {
            "categories": [CategorySummary, ...],   # one per asset/SEBI category
            "suggestions": [SwitchSuggestion, ...],  # actionable switch ideas
            "stats": {
                "total_holdings_analysed", "low_yield_count",
                "potential_uplift_total", "categories_count",
            }
        }
    """
    # 1. Collect every holding across every client (with current value)
    accounts = session.execute(select(Account)).scalars().all()
    all_rows: list[tuple[Account, Any]] = []
    for acc in accounts:
        holdings = compute_client_holdings(session, acc.id)
        if nav_lookup:
            enrich_with_current_value(holdings, nav_lookup)
        for h in holdings:
            if h.units_held > 0:
                all_rows.append((acc, h))

    # 2. Group by category, compute per-category leader
    by_cat: dict[str, list[tuple[Account, Any]]] = defaultdict(list)
    for acc, h in all_rows:
        by_cat[_category_of(h)].append((acc, h))

    categories: list[CategorySummary] = []
    leaders: dict[str, tuple[str, float]] = {}  # category -> (scheme name, return %)

    for cat, rows in by_cat.items():
        # AUM-weighted average return for this category
        weighted_sum = 0.0
        weight_total = 0.0
        cat_aum = 0.0
        best_return = float("-inf")
        best_scheme = ""
        for _, h in rows:
            cv = h.current_value or 0.0
            cat_aum += cv
            ret = _holding_return_pct(h)
            if ret is not None and cv > 0:
                weighted_sum += ret * cv
                weight_total += cv
                if ret > best_return:
                    best_return = ret
                    best_scheme = h.scheme_name
        avg_ret = (weighted_sum / weight_total) if weight_total else 0.0
        if best_return == float("-inf"):
            best_return = 0.0
        leaders[cat] = (best_scheme, best_return)
        categories.append(CategorySummary(
            category=cat,
            holdings_count=len(rows),
            aum=cat_aum,
            avg_return_pct=round(avg_ret, 2),
            leader_scheme=best_scheme,
            leader_return_pct=round(best_return, 2),
        ))

    categories.sort(key=lambda c: c.aum, reverse=True)

    # 3. Build switch suggestions for laggard holdings
    suggestions: list[SwitchSuggestion] = []
    for acc, h in all_rows:
        cat = _category_of(h)
        invested = float(h.invested or 0)
        if invested < MIN_INVESTED:
            continue
        held_ret = _holding_return_pct(h)
        if held_ret is None:
            continue

        leader_scheme, leader_ret = leaders.get(cat, ("", 0.0))
        # Don't recommend the same scheme as itself
        if leader_scheme == h.scheme_name or leader_ret <= 0:
            continue
        gap = leader_ret - held_ret
        if gap < LAG_THRESHOLD_PPTS:
            continue

        uplift = invested * gap / 100.0
        if gap >= 10:
            severity = "high"
        elif gap >= 7:
            severity = "medium"
        else:
            severity = "low"

        suggestions.append(SwitchSuggestion(
            client_pan=acc.pan or "",
            client_name=acc.name,
            held_scheme=h.scheme_name,
            held_scheme_code=h.scheme_code,
            held_isin=h.isin,
            category=cat,
            invested=invested,
            current_value=float(h.current_value or 0),
            held_return_pct=round(held_ret, 2),
            leader_scheme=leader_scheme,
            leader_return_pct=round(leader_ret, 2),
            return_gap_pct=round(gap, 2),
            potential_uplift_1y=round(uplift, 2),
            severity=severity,
        ))

    suggestions.sort(key=lambda s: s.potential_uplift_1y, reverse=True)

    return {
        "categories": categories,
        "suggestions": suggestions,
        "stats": {
            "total_holdings_analysed": len(all_rows),
            "low_yield_count": len(suggestions),
            "potential_uplift_total": round(sum(s.potential_uplift_1y for s in suggestions), 2),
            "categories_count": len(categories),
        },
    }
