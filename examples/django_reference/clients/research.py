"""Research workspace — market news + AI-powered insights.

Two pieces:

1. **Market trends feed** — a curated list of headlines that the advisor
   would normally read across Moneycontrol / Economic Times / LiveMint /
   AMFI. We render a static-but-realistic feed (refreshable timestamps);
   in production this would call each source's RSS endpoint.

2. **AI-powered insights** — a heuristic recommendation engine that
   analyses the book and surfaces actionable insights. We call it
   "AI-powered" because the rules mirror what a finance LLM would
   highlight: concentration risk, missing asset classes, dormant SIPs,
   tax-loss harvesting opportunities, etc.
"""
from __future__ import annotations

import datetime as dt
from collections import defaultdict
from typing import Any

from sqlalchemy import select

from openreversefeed.db.models import Account, Scheme

from . import research_rules
from .portfolio import (
    classify_asset,
    compute_client_holdings,
    enrich_with_current_value,
)


# ─────────────────────────────────────────────────────────────────────
# Market news feed
# ─────────────────────────────────────────────────────────────────────

# Curated headlines. In production this would be RSS-driven. Each entry:
#   (title, source, url, category, ago_minutes)
_NEWS_SEED: list[tuple[str, str, str, str, int]] = [
    ("Sensex closes 384 pts higher; IT, banking lead gains as RBI holds rates",
     "Economic Times", "https://economictimes.indiatimes.com/markets", "equity", 22),
    ("Mid-cap funds outperform large-caps for 4th straight quarter, AMFI data shows",
     "Moneycontrol", "https://www.moneycontrol.com/mutualfundindia/", "mutual_funds", 47),
    ("Foreign inflows into Indian debt cross ₹65,000 cr in April — JP Morgan index effect",
     "LiveMint", "https://www.livemint.com/market", "debt", 95),
    ("RBI MPC keeps repo at 6.50%; signals shift to neutral stance",
     "Reuters India", "https://www.reuters.com/world/india/", "macro", 110),
    ("SEBI tightens scrutiny on mid/small-cap MF schemes amid frothy valuations",
     "Business Standard", "https://www.business-standard.com/markets", "regulation", 145),
    ("Gold ETFs see ₹1,200 cr inflow in March, highest in 2 years — AMFI",
     "AMFI India", "https://www.amfiindia.com/", "commodity", 178),
    ("Equity SIP inflows hit fresh record at ₹22,800 cr in April",
     "AMFI India", "https://www.amfiindia.com/", "sip", 220),
    ("US Fed pivots dovish; emerging market equities rally as DXY softens",
     "Bloomberg", "https://www.bloomberg.com/asia", "global", 295),
    ("Hybrid funds gain favour: balanced advantage AUM up 18% YTD",
     "Moneycontrol", "https://www.moneycontrol.com/mutualfundindia/", "hybrid", 320),
    ("Q4 GDP print at 7.6% beats consensus; manufacturing surprises positively",
     "Mint", "https://www.livemint.com/economy", "macro", 425),
    ("Liquid-fund yields slip to 6.8% as banking-system liquidity improves",
     "Business Standard", "https://www.business-standard.com/markets", "debt", 510),
    ("Direct-plan AUM crosses 50% mark for the first time, says AMFI",
     "Economic Times", "https://economictimes.indiatimes.com/wealth", "industry", 600),
]

NEWS_CATEGORIES = [
    ("all",          "All news"),
    ("equity",       "Equity"),
    ("debt",         "Debt"),
    ("hybrid",       "Hybrid"),
    ("commodity",    "Commodity"),
    ("mutual_funds", "Mutual Funds"),
    ("sip",          "SIP / Flows"),
    ("macro",        "Macro"),
    ("regulation",   "Regulation"),
    ("global",       "Global"),
    ("industry",     "Industry"),
]


def get_market_news(category: str = "all") -> list[dict[str, Any]]:
    """Render the news feed with up-to-the-minute timestamps.

    Filters by category; "all" returns everything (newest first by minutes-ago).
    """
    now = dt.datetime.now()
    items = []
    for title, source, url, cat, ago in _NEWS_SEED:
        if category != "all" and cat != category:
            continue
        published = now - dt.timedelta(minutes=ago)
        items.append({
            "title": title,
            "source": source,
            "url": url,
            "category": cat,
            "category_label": dict(NEWS_CATEGORIES).get(cat, cat.title()),
            "published_at": published,
            "ago": _humanise_ago(ago),
        })
    items.sort(key=lambda x: x["published_at"], reverse=True)
    return items


def _humanise_ago(minutes: int) -> str:
    if minutes < 60:
        return f"{minutes}m ago"
    if minutes < 60 * 24:
        return f"{minutes // 60}h ago"
    return f"{minutes // (60 * 24)}d ago"


# ─────────────────────────────────────────────────────────────────────
# AI-powered insights — heuristic engine over the live book
# ─────────────────────────────────────────────────────────────────────


def compute_client_insights(session, account, nav_lookup, rules: dict[str, float] | None = None) -> dict[str, Any]:
    """Run the rule engine for ONE client. Used for on-demand generation.

    Returns: {
        "pan": str,
        "name": str,
        "aum": float,
        "insights": [insight_dict, ...]   # sorted high → low severity
    }
    """
    if rules is None:
        rules = research_rules.effective_rules()

    holdings = compute_client_holdings(session, account.id)
    enrich_with_current_value(holdings, nav_lookup)
    active = [h for h in holdings if h.units_held > 0]
    client_aum = sum(float(h.current_value or 0) for h in active)
    client_insights: list[dict[str, Any]] = []

    if not active:
        return {
            "pan": account.pan or "",
            "name": account.name,
            "aum": 0.0,
            "insights": [],
        }

    # 1. Concentration
    if client_aum > 0:
        top = max(active, key=lambda h: float(h.current_value or 0))
        top_pct = float(top.current_value or 0) / client_aum * 100
        if top_pct > rules["concentration_pct"]:
            sev = "high" if top_pct > rules["concentration_severity_high_pct"] else "medium"
            client_insights.append({
                "kind": "concentration",
                "rule_key": "concentration_pct",
                "severity": sev,
                "title": f"{top_pct:.0f}% concentrated in {top.scheme_name[:48]}",
                "detail": (
                    f"A single scheme accounts for ₹{float(top.current_value or 0):,.0f} of "
                    f"₹{client_aum:,.0f} AUM. Idiosyncratic risk: any drawdown in this "
                    f"fund disproportionately impacts the portfolio."
                ),
                "action": "Trim the position and redeploy into 2-3 diversified funds in the same category.",
            })

    # 2. Missing debt
    held_classes = {classify_asset(h.scheme_name) for h in active}
    if (
        "Equity" in held_classes
        and "Debt" not in held_classes
        and client_aum > rules["missing_debt_min_aum"]
    ):
        client_insights.append({
            "kind": "missing_class",
            "rule_key": "missing_debt_min_aum",
            "severity": "medium",
            "title": "No debt allocation in portfolio",
            "detail": (
                f"Portfolio is 100% equity (₹{client_aum:,.0f}). Adding a 20-30% debt "
                f"sleeve would smooth drawdowns and improve risk-adjusted returns."
            ),
            "action": "Suggest a short-duration debt fund or banking & PSU debt fund.",
        })

    # 3. Underperformers
    for h in active:
        if h.xirr_pct is not None and h.xirr_pct < rules["underperformer_xirr_pct"]:
            client_insights.append({
                "kind": "underperformer",
                "rule_key": "underperformer_xirr_pct",
                "severity": "medium",
                "title": f"{h.scheme_name[:46]} — XIRR {h.xirr_pct:.1f}%",
                "detail": (
                    f"Losing money on an annualised basis "
                    f"(invested ₹{float(h.invested):,.0f}, currently ₹"
                    f"{float(h.current_value or 0):,.0f})."
                ),
                "action": "Review against benchmark; switch if 3-yr underperformance is structural.",
            })

    # 4. Tax-loss harvesting
    for h in active:
        if h.gain_loss is not None and h.gain_loss < -rules["tax_loss_min"]:
            client_insights.append({
                "kind": "tax_loss",
                "rule_key": "tax_loss_min",
                "severity": "low",
                "title": f"₹{abs(h.gain_loss):,.0f} unrealised loss in {h.scheme_name[:36]}",
                "detail": "This loss can offset capital gains booked elsewhere this FY.",
                "action": "Consider tax-loss harvesting before 31 Mar; rebuy after 30+ days.",
            })
            break

    # 5. Category laggard
    cat_leader: dict[str, tuple[str, float]] = {}
    for h in active:
        cat = getattr(h, "sebi_category", None) or classify_asset(h.scheme_name) or "Other"
        ret = h.xirr_pct if h.xirr_pct is not None else h.gain_loss_pct
        if ret is None:
            continue
        curr = cat_leader.get(cat)
        if curr is None or ret > curr[1]:
            cat_leader[cat] = (h.scheme_name, ret)
    for h in active:
        cat = getattr(h, "sebi_category", None) or classify_asset(h.scheme_name) or "Other"
        ret = h.xirr_pct if h.xirr_pct is not None else h.gain_loss_pct
        if ret is None or float(h.invested or 0) < 50_000:
            continue
        leader = cat_leader.get(cat)
        if not leader or leader[0] == h.scheme_name or leader[1] <= 0:
            continue
        gap = leader[1] - ret
        if gap >= rules["category_lag_ppts"]:
            client_insights.append({
                "kind": "category_lag",
                "rule_key": "category_lag_ppts",
                "severity": "low",
                "title": f"{h.scheme_name[:42]} lags category leader by {gap:.1f} ppts",
                "detail": (
                    f"Category {cat}: this fund returns {ret:.1f}% vs {leader[1]:.1f}% "
                    f"for {leader[0][:46]}."
                ),
                "action": f"Switch consideration: → {leader[0][:46]}.",
            })
            break

    severity_rank = {"high": 0, "medium": 1, "low": 2}
    client_insights.sort(key=lambda x: severity_rank[x["severity"]])

    return {
        "pan": account.pan or "",
        "name": account.name,
        "aum": client_aum,
        "insights": client_insights,
    }


def compute_book_wide_insights(session, nav_lookup, rules: dict[str, float] | None = None) -> list[dict[str, Any]]:
    """Macro insights that need a full pass over the book (expensive at scale,
    but kept because the advisor wants them visible by default).

    Returns the list of book-wide insights only."""
    if rules is None:
        rules = research_rules.effective_rules()

    book_total_aum = 0.0
    asset_totals: dict[str, float] = defaultdict(float)
    accounts = session.execute(select(Account)).scalars().all()
    for acc in accounts:
        holdings = compute_client_holdings(session, acc.id)
        enrich_with_current_value(holdings, nav_lookup)
        for h in holdings:
            if h.units_held <= 0:
                continue
            cv = float(h.current_value or 0)
            book_total_aum += cv
            asset_totals[classify_asset(h.scheme_name)] += cv

    book_wide: list[dict[str, Any]] = []
    if book_total_aum > 0:
        equity_pct = asset_totals.get("Equity", 0) / book_total_aum * 100
        if equity_pct > rules["book_equity_skew_pct"]:
            book_wide.append({
                "kind": "book_skew",
                "rule_key": "book_equity_skew_pct",
                "severity": "high",
                "title": f"Book is {equity_pct:.0f}% equity — high market sensitivity",
                "detail": (
                    f"The entire ARN book is heavily tilted toward equity (₹"
                    f"{asset_totals.get('Equity', 0):,.0f} of ₹{book_total_aum:,.0f}). "
                    f"A 20% market correction would crystallise material drawdown across "
                    f"clients simultaneously."
                ),
                "action": "Run a portfolio review wave — pitch hybrid funds to growth-tolerant clients.",
            })

        debt_pct = asset_totals.get("Debt", 0) / book_total_aum * 100
        if debt_pct < rules["book_debt_floor_pct"] and book_total_aum > rules["book_skew_min_aum"]:
            book_wide.append({
                "kind": "book_skew",
                "rule_key": "book_debt_floor_pct",
                "severity": "medium",
                "title": f"Only {debt_pct:.0f}% in debt across the book",
                "detail": (
                    "Debt allocation is unusually thin. Yields on short-duration debt "
                    "are ~7%, attractive for clients sitting in liquid funds at 6.8%."
                ),
                "action": "Pitch arbitrage / short-duration debt as a tax-efficient liquid alternative.",
            })

    book_wide.append({
        "kind": "macro",
        "rule_key": None,
        "severity": "low",
        "title": "Equity LTCG threshold reminder — FY 25-26",
        "detail": (
            "The LTCG exemption on equity is ₹1.25 L per person per year. "
            "Across the book, schedule small redemptions before 31 Mar to use it up."
        ),
        "action": "Identify clients with ≥ ₹1.25 L unrealised long-term equity gain.",
    })

    return book_wide


def list_clients_lite(session) -> list[dict[str, Any]]:
    """Cheap list of every account (just name + PAN) — for the paginated
    client picker. No holdings/NAV/XIRR work performed."""
    accounts = session.execute(select(Account).order_by(Account.name)).scalars().all()
    return [
        {"pan": a.pan or "", "name": a.name, "id": str(a.id)}
        for a in accounts
    ]


def generate_ai_insights(session, nav_lookup) -> dict[str, Any]:
    """Walk the book and produce actionable insights, grouped by client.

    Returns:
        {
            "by_client": [
                {
                    "pan": str,
                    "name": str,
                    "aum": float,
                    "insight_count": int,
                    "max_severity": "high" | "medium" | "low" | None,
                    "insights": [insight_dict, ...],
                }, ...
            ],
            "book_wide": [insight_dict, ...],
            "summary": {
                "total_clients": int,
                "clients_with_insights": int,
                "total_insights": int,
                "sev_counts": {"high": int, "medium": int, "low": int},
            },
            "rules_used": {key: value, ...},   # for transparency in the UI
        }
    """
    rules = research_rules.effective_rules()
    accounts = session.execute(select(Account)).scalars().all()

    by_client: list[dict[str, Any]] = []
    book_wide: list[dict[str, Any]] = []
    book_total_aum = 0.0
    asset_totals: dict[str, float] = defaultdict(float)
    severity_rank = {"high": 0, "medium": 1, "low": 2}

    for acc in accounts:
        holdings = compute_client_holdings(session, acc.id)
        enrich_with_current_value(holdings, nav_lookup)
        active = [h for h in holdings if h.units_held > 0]
        if not active:
            continue
        client_aum = sum(float(h.current_value or 0) for h in active)
        book_total_aum += client_aum

        client_insights: list[dict[str, Any]] = []

        # 1. Concentration: top scheme > threshold% of client AUM
        if client_aum > 0:
            top = max(active, key=lambda h: float(h.current_value or 0))
            top_pct = float(top.current_value or 0) / client_aum * 100
            if top_pct > rules["concentration_pct"]:
                sev = "high" if top_pct > rules["concentration_severity_high_pct"] else "medium"
                client_insights.append({
                    "kind": "concentration",
                    "rule_key": "concentration_pct",
                    "severity": sev,
                    "title": f"{top_pct:.0f}% concentrated in {top.scheme_name[:48]}",
                    "detail": (
                        f"A single scheme accounts for ₹{float(top.current_value or 0):,.0f} of "
                        f"₹{client_aum:,.0f} AUM. Idiosyncratic risk: any drawdown in this "
                        f"fund disproportionately impacts the portfolio."
                    ),
                    "action": "Trim the position and redeploy into 2-3 diversified funds in the same category.",
                })

        # 2. Missing debt allocation
        held_classes = {classify_asset(h.scheme_name) for h in active}
        if (
            "Equity" in held_classes
            and "Debt" not in held_classes
            and client_aum > rules["missing_debt_min_aum"]
        ):
            client_insights.append({
                "kind": "missing_class",
                "rule_key": "missing_debt_min_aum",
                "severity": "medium",
                "title": "No debt allocation in portfolio",
                "detail": (
                    f"Portfolio is 100% equity (₹{client_aum:,.0f}). Adding a 20-30% debt "
                    f"sleeve would smooth drawdowns and improve risk-adjusted returns."
                ),
                "action": "Suggest a short-duration debt fund or banking & PSU debt fund.",
            })

        # 3. Underperformers (XIRR below threshold)
        for h in active:
            if h.xirr_pct is not None and h.xirr_pct < rules["underperformer_xirr_pct"]:
                client_insights.append({
                    "kind": "underperformer",
                    "rule_key": "underperformer_xirr_pct",
                    "severity": "medium",
                    "title": f"{h.scheme_name[:46]} — XIRR {h.xirr_pct:.1f}%",
                    "detail": (
                        f"Losing money on an annualised basis "
                        f"(invested ₹{float(h.invested):,.0f}, currently ₹"
                        f"{float(h.current_value or 0):,.0f})."
                    ),
                    "action": "Review against benchmark; switch if 3-yr underperformance is structural.",
                })

        # 4. Tax-loss harvesting candidate
        for h in active:
            if h.gain_loss is not None and h.gain_loss < -rules["tax_loss_min"]:
                client_insights.append({
                    "kind": "tax_loss",
                    "rule_key": "tax_loss_min",
                    "severity": "low",
                    "title": f"₹{abs(h.gain_loss):,.0f} unrealised loss in {h.scheme_name[:36]}",
                    "detail": (
                        "This loss can offset capital gains booked elsewhere this FY."
                    ),
                    "action": "Consider tax-loss harvesting before 31 Mar; rebuy after 30+ days.",
                })
                break  # one per client

        # 5. Category laggard (compare to best holding in same category)
        cat_leader: dict[str, tuple[str, float]] = {}  # category → (scheme, return%)
        for h in active:
            cat = getattr(h, "sebi_category", None) or classify_asset(h.scheme_name) or "Other"
            ret = h.xirr_pct if h.xirr_pct is not None else h.gain_loss_pct
            if ret is None:
                continue
            curr = cat_leader.get(cat)
            if curr is None or ret > curr[1]:
                cat_leader[cat] = (h.scheme_name, ret)
        for h in active:
            cat = getattr(h, "sebi_category", None) or classify_asset(h.scheme_name) or "Other"
            ret = h.xirr_pct if h.xirr_pct is not None else h.gain_loss_pct
            if ret is None or float(h.invested or 0) < 50_000:
                continue
            leader = cat_leader.get(cat)
            if not leader or leader[0] == h.scheme_name or leader[1] <= 0:
                continue
            gap = leader[1] - ret
            if gap >= rules["category_lag_ppts"]:
                client_insights.append({
                    "kind": "category_lag",
                    "rule_key": "category_lag_ppts",
                    "severity": "low",
                    "title": f"{h.scheme_name[:42]} lags category leader by {gap:.1f} ppts",
                    "detail": (
                        f"Category {cat}: this fund returns {ret:.1f}% vs {leader[1]:.1f}% "
                        f"for {leader[0][:46]}."
                    ),
                    "action": f"Switch consideration: → {leader[0][:46]}.",
                })
                break  # one laggard insight per client to avoid noise

        # Sort this client's insights by severity then accumulate
        client_insights.sort(key=lambda x: severity_rank[x["severity"]])
        max_sev = client_insights[0]["severity"] if client_insights else None
        by_client.append({
            "pan": acc.pan or "",
            "name": acc.name,
            "aum": client_aum,
            "insight_count": len(client_insights),
            "max_severity": max_sev,
            "insights": client_insights,
        })

        for h in active:
            asset_totals[classify_asset(h.scheme_name)] += float(h.current_value or 0)

    # ── Book-wide insights ────────────────────────────────────────
    if book_total_aum > 0:
        equity_pct = asset_totals.get("Equity", 0) / book_total_aum * 100
        if equity_pct > rules["book_equity_skew_pct"]:
            book_wide.append({
                "kind": "book_skew",
                "rule_key": "book_equity_skew_pct",
                "severity": "high",
                "title": f"Book is {equity_pct:.0f}% equity — high market sensitivity",
                "detail": (
                    f"The entire ARN book is heavily tilted toward equity (₹"
                    f"{asset_totals.get('Equity', 0):,.0f} of ₹{book_total_aum:,.0f}). "
                    f"A 20% market correction would crystallise material drawdown across "
                    f"clients simultaneously."
                ),
                "action": "Run a portfolio review wave — pitch hybrid funds to growth-tolerant clients.",
            })

        debt_pct = asset_totals.get("Debt", 0) / book_total_aum * 100
        if debt_pct < rules["book_debt_floor_pct"] and book_total_aum > rules["book_skew_min_aum"]:
            book_wide.append({
                "kind": "book_skew",
                "rule_key": "book_debt_floor_pct",
                "severity": "medium",
                "title": f"Only {debt_pct:.0f}% in debt across the book",
                "detail": (
                    "Debt allocation is unusually thin. Yields on short-duration debt "
                    "are ~7%, attractive for clients sitting in liquid funds at 6.8%."
                ),
                "action": "Pitch arbitrage / short-duration debt as a tax-efficient liquid alternative.",
            })

    book_wide.append({
        "kind": "macro",
        "rule_key": None,
        "severity": "low",
        "title": "Equity LTCG threshold reminder — FY 25-26",
        "detail": (
            "The LTCG exemption on equity is ₹1.25 L per person per year. "
            "Across the book, schedule small redemptions before 31 Mar to use it up."
        ),
        "action": "Identify clients with ≥ ₹1.25 L unrealised long-term equity gain.",
    })

    # Sort by-client list: most-severe first, then by insight count, then AUM
    sev_to_n = {"high": 0, "medium": 1, "low": 2, None: 3}
    by_client.sort(key=lambda c: (sev_to_n[c["max_severity"]], -c["insight_count"], -c["aum"]))

    sev_counts = {"high": 0, "medium": 0, "low": 0}
    total_insights = 0
    clients_with_insights = 0
    for c in by_client:
        if c["insight_count"]:
            clients_with_insights += 1
        for ins in c["insights"]:
            sev_counts[ins["severity"]] += 1
            total_insights += 1
    for ins in book_wide:
        sev_counts[ins["severity"]] += 1
        total_insights += 1

    return {
        "by_client": by_client,
        "book_wide": book_wide,
        "summary": {
            "total_clients": len(by_client),
            "clients_with_insights": clients_with_insights,
            "total_insights": total_insights,
            "sev_counts": sev_counts,
        },
        "rules_used": rules,
    }


# ─────────────────────────────────────────────────────────────────────
# Scheme universe for the fund-trend search
# ─────────────────────────────────────────────────────────────────────

def list_searchable_schemes(session) -> list[dict[str, Any]]:
    """Every scheme in our DB, lightweight payload for the search picker.
    Includes AMC name so the funds sub-tab can group schemes by AMC."""
    from openreversefeed.db.models import Amc
    amc_lookup = {a.id: a.name for a in session.execute(select(Amc)).scalars().all()}
    schemes = session.execute(
        select(Scheme).order_by(Scheme.name)
    ).scalars().all()
    out = []
    for s in schemes:
        meta = s.meta or {}
        out.append({
            "scheme_code": s.scheme_code,
            "name": s.name,
            "isin": s.isin or "",
            "category": meta.get("sebi_category") or meta.get("nature") or "",
            "asset_class": meta.get("asset_class") or "",
            "amc_id": s.amc_id,
            "amc_name": amc_lookup.get(s.amc_id, "Other"),
        })
    return out


def group_schemes_by_amc(schemes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group a flat scheme list by AMC. Returns a list of:
        {"amc_name": str, "count": int, "schemes": [scheme_dict, ...]}
    Sorted by AMC name (alphabetical), schemes within sorted by name.
    """
    from collections import defaultdict
    bucket: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for s in schemes:
        bucket[s.get("amc_name") or "Other"].append(s)
    grouped = []
    for amc_name in sorted(bucket.keys(), key=lambda x: x.lower()):
        items = sorted(bucket[amc_name], key=lambda x: x["name"].lower())
        grouped.append({
            "amc_name": amc_name,
            "count": len(items),
            "schemes": items,
        })
    return grouped
