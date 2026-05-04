"""ARN-level dashboard computations.

All clients in the system are assumed to belong to a single ARN (the user
running the app). This module aggregates per-client portfolios into the
firm-level metrics shown on the ARN dashboard.

Notes on accuracy:
* Realized / unrealized gains use the same weighted-average cost basis logic
  as the per-client portfolio computation.
* STCG / LTCG split uses **FIFO tax-lot tracking** (separate from the
  weighted-avg accounting). This is the correct way per income-tax rules.
  The threshold is 12 months for equity / hybrid / arbitrage / index funds,
  and 36 months for pure debt funds (per pre-Apr-2023 rules; the post-2023
  amendments now treat new debt purchases entirely as STCG — we still use 36m
  for simplicity since holdings are mixed).
* Trail income is an estimate using a configurable blended trail rate.
"""
from __future__ import annotations

import datetime as dt
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import select

from openreversefeed.db.models import (
    Account,
    Amc,
    Folio,
    Scheme,
    SourceFile,
    Transaction,
)

from .amfi_nav import lookup_nav, load_nav_map
from .portfolio import classify_asset, compute_xirr

# Approximate blended trail commission (industry average ~0.6% to 1% p.a.).
DEFAULT_TRAIL_RATE = 0.0075  # 0.75% p.a. blended

# Equity-side categories use 12-month STCG threshold; debt-side uses 36 months.
_EQUITY_LIKE = {"Equity", "Hybrid", "Commodity"}


def _holding_period_threshold_months(asset_class: str) -> int:
    """Return STCG cutoff in months for the asset class."""
    return 12 if asset_class in _EQUITY_LIKE else 36


@dataclass
class _SchemeRollup:
    scheme_id: int
    scheme_code: str
    scheme_name: str
    isin: str | None
    amc_code: str
    amc_name: str
    asset_class: str
    units: float
    cost_basis: float
    realized: float
    stcg: float
    ltcg: float
    nav: float | None
    nav_date: str | None
    current_value: float | None  # None if no NAV match
    accounts: set


def compute_arn_dashboard(session, top_n: int = 10) -> dict[str, Any]:
    today = dt.date.today()

    # ── Pull every transaction with scheme + amc + folio + source file in one shot
    rows = session.execute(
        select(Transaction, Scheme, Amc, Folio, SourceFile, Account)
        .join(Scheme, Scheme.id == Transaction.scheme_id)
        .join(Amc, Amc.id == Scheme.amc_id)
        .join(Folio, Folio.id == Transaction.folio_id)
        .join(SourceFile, SourceFile.id == Transaction.source_file_id)
        .join(Account, Account.id == Transaction.account_id)
        .order_by(Transaction.transaction_date, Transaction.id)
    ).all()

    # Group transactions by (account, scheme) — chronological within each
    by_acct_scheme: dict[tuple, list] = defaultdict(list)
    scheme_meta: dict[int, dict] = {}
    folios_by_account: dict = defaultdict(set)
    accounts_seen: set = set()

    for t, s, a, f, sf, acct in rows:
        by_acct_scheme[(acct.id, s.id)].append((t, s, a, f, acct))
        scheme_meta[s.id] = {
            "scheme_code": s.scheme_code,
            "name": s.name or s.scheme_code,
            "isin": s.isin,
            "amc_code": a.code,
            "amc_name": a.name,
            "asset_class": (s.meta or {}).get("asset_class") or classify_asset(s.name),
            "category": (s.meta or {}).get("nature"),
        }
        folios_by_account[acct.id].add(f.id)
        accounts_seen.add(acct.id)

    # ── Per-(account, scheme) accounting: weighted-avg cost + FIFO tax lots
    # FIFO lot = (date, units, cost_per_unit). Sell consumes oldest first.
    # Weighted-avg cost is also tracked for "Avg Cost / Invested" display.
    per_combo: dict[tuple, dict] = {}

    for (acct_id, sch_id), txns in by_acct_scheme.items():
        meta = scheme_meta[sch_id]
        threshold_months = _holding_period_threshold_months(meta["asset_class"])

        # weighted avg
        wa_units = Decimal("0")
        wa_cost = Decimal("0")
        wa_realized = Decimal("0")
        # FIFO
        lots: list[list] = []  # each lot: [date, units, cost_per_unit]
        stcg = Decimal("0")
        ltcg = Decimal("0")

        for t, s, a, f, acct in txns:
            if t.action == "buy":
                wa_units += t.units
                wa_cost += t.amount
                if t.units > 0:
                    cost_per_unit = t.amount / t.units
                    lots.append([t.transaction_date, t.units, cost_per_unit])
            elif t.action == "sell":
                # Weighted-avg accounting
                avg = wa_cost / wa_units if wa_units > 0 else Decimal("0")
                units_with_cost = (
                    min(t.units, wa_units) if wa_units > 0 else Decimal("0")
                )
                wa_realized += t.amount - (units_with_cost * avg)
                wa_units -= t.units
                wa_cost -= units_with_cost * avg
                if wa_units < 0:
                    wa_units = Decimal("0")
                    wa_cost = Decimal("0")

                # FIFO tax-lot consumption for STCG/LTCG
                remaining_to_sell = t.units
                # Realised proceeds spread proportionally over the lots consumed
                while remaining_to_sell > 0 and lots:
                    lot_date, lot_units, lot_cost_per_unit = lots[0]
                    consumed = min(remaining_to_sell, lot_units)
                    cost_consumed = consumed * lot_cost_per_unit
                    proceeds_share = (
                        (consumed / t.units) * t.amount if t.units > 0 else Decimal("0")
                    )
                    gain = proceeds_share - cost_consumed
                    months_held = _months_between(lot_date, t.transaction_date)
                    if months_held >= threshold_months:
                        ltcg += gain
                    else:
                        stcg += gain

                    lots[0][1] -= consumed
                    if lots[0][1] <= 0:
                        lots.pop(0)
                    remaining_to_sell -= consumed

        # Net out remaining tiny dust
        if wa_units <= Decimal("0"):
            wa_units = Decimal("0")
            wa_cost = Decimal("0")

        # Current NAV → current value
        nav_val, nav_date, _ = lookup_nav(meta["scheme_code"], meta["name"], meta["isin"])
        current_value = (
            float(wa_units) * nav_val if (nav_val is not None and wa_units > 0) else None
        )

        per_combo[(acct_id, sch_id)] = {
            "units": float(wa_units),
            "cost_basis": float(wa_cost),
            "realized": float(wa_realized),
            "stcg": float(stcg),
            "ltcg": float(ltcg),
            "nav": nav_val,
            "nav_date": nav_date,
            "current_value": current_value,
            "amc_code": meta["amc_code"],
            "amc_name": meta["amc_name"],
            "scheme_id": sch_id,
            "scheme_code": meta["scheme_code"],
            "scheme_name": meta["name"],
            "isin": meta["isin"],
            "asset_class": meta["asset_class"],
            "category": meta["category"],
            "first_txn": min(t.transaction_date for t, *_ in txns),
            "last_txn": max(t.transaction_date for t, *_ in txns),
            "txns": txns,
        }

    # ── Roll up to per-account
    per_account: dict = defaultdict(lambda: {
        "name": "",
        "pan": "",
        "invested": 0.0,
        "current_value": 0.0,
        "realized": 0.0,
        "unrealized": 0.0,
        "stcg": 0.0,
        "ltcg": 0.0,
        "redemptions_amount": 0.0,
        "purchases_amount": 0.0,
        "txn_count": 0,
        "first_txn": None,
        "last_txn": None,
        "schemes_held": 0,
        "scheme_codes_active": set(),
        "all_schemes": set(),
        "any_priced": False,
        "all_priced_cost": 0.0,
    })

    for (acct_id, sch_id), c in per_combo.items():
        bucket = per_account[acct_id]
        bucket["invested"] += c["cost_basis"]
        bucket["realized"] += c["realized"]
        bucket["stcg"] += c["stcg"]
        bucket["ltcg"] += c["ltcg"]
        bucket["all_schemes"].add(sch_id)
        if c["units"] > 0:
            bucket["scheme_codes_active"].add(sch_id)
            if c["current_value"] is not None:
                bucket["current_value"] += c["current_value"]
                bucket["any_priced"] = True
                bucket["all_priced_cost"] += c["cost_basis"]
                bucket["unrealized"] += c["current_value"] - c["cost_basis"]

        if bucket["first_txn"] is None or c["first_txn"] < bucket["first_txn"]:
            bucket["first_txn"] = c["first_txn"]
        if bucket["last_txn"] is None or c["last_txn"] > bucket["last_txn"]:
            bucket["last_txn"] = c["last_txn"]

    # Account-level inflows/outflows
    cashflows_per_account: dict = defaultdict(list)
    for (acct_id, sch_id), c in per_combo.items():
        for t, s, a, f, acct in c["txns"]:
            per_account[acct_id]["txn_count"] += 1
            if t.action == "buy":
                per_account[acct_id]["purchases_amount"] += float(t.amount)
                cashflows_per_account[acct_id].append(
                    (t.transaction_date, -float(t.amount))
                )
            elif t.action == "sell":
                per_account[acct_id]["redemptions_amount"] += float(t.amount)
                cashflows_per_account[acct_id].append(
                    (t.transaction_date, float(t.amount))
                )

    # Account names
    acct_rows = session.execute(select(Account)).scalars().all()
    for a in acct_rows:
        if a.id in per_account:
            per_account[a.id]["name"] = a.name
            per_account[a.id]["pan"] = a.pan or ""
            per_account[a.id]["account_id"] = a.id
        elif a.id in accounts_seen:
            per_account[a.id]["name"] = a.name
            per_account[a.id]["pan"] = a.pan or ""
            per_account[a.id]["account_id"] = a.id

    # XIRR per account
    for acct_id, b in per_account.items():
        flows = list(cashflows_per_account.get(acct_id, []))
        if b["current_value"]:
            flows.append((today, b["current_value"]))
        x = compute_xirr(flows) if len(flows) >= 2 else None
        b["xirr_pct"] = round(x * 100, 2) if x is not None else None
        b["schemes_held"] = len(b["scheme_codes_active"])
        b["total_gain"] = b["realized"] + b["unrealized"]

    # ── Headline metrics
    total_aum = sum(b["current_value"] for b in per_account.values())
    total_invested = sum(b["invested"] for b in per_account.values())
    total_realized = sum(b["realized"] for b in per_account.values())
    total_unrealized = sum(b["unrealized"] for b in per_account.values())
    total_stcg = sum(b["stcg"] for b in per_account.values())
    total_ltcg = sum(b["ltcg"] for b in per_account.values())
    total_purchases = sum(b["purchases_amount"] for b in per_account.values())
    total_redemptions = sum(b["redemptions_amount"] for b in per_account.values())
    total_capital_losses = sum(min(b["realized"], 0) for b in per_account.values())

    # Capital loss = sum of NEGATIVE realized gains (across all combos, not netted)
    total_capital_losses = sum(
        c["realized"] for c in per_combo.values() if c["realized"] < 0
    )

    # ── Client status
    total_clients = len(per_account)
    active_clients = sum(
        1 for b in per_account.values() if b["current_value"] and b["current_value"] > 0
    )
    zero_aum_clients = total_clients - active_clients
    one_year_ago = today - dt.timedelta(days=365)
    six_months_ago = today - dt.timedelta(days=180)
    new_clients = sum(
        1 for b in per_account.values()
        if b["first_txn"] and b["first_txn"] >= six_months_ago
    )
    dormant_clients = [
        {"name": b["name"], "pan": b["pan"], "last_txn": b["last_txn"]}
        for b in per_account.values()
        if b["last_txn"] and b["last_txn"] < one_year_ago
    ]
    dormant_clients.sort(key=lambda c: c["last_txn"] or dt.date.min)

    # Retention rate: fraction of clients who transacted both in current quarter and previous
    cur_q_start = today - dt.timedelta(days=90)
    prev_q_start = today - dt.timedelta(days=180)
    active_cur = {
        acct_id for acct_id, b in per_account.items()
        if b["last_txn"] and b["last_txn"] >= cur_q_start
    }
    active_prev = {
        acct_id for acct_id, b in per_account.items()
        if b["last_txn"] and prev_q_start <= b["last_txn"] < cur_q_start
    }
    retained = active_cur & active_prev
    retention_rate = (
        len(retained) / len(active_prev) * 100 if active_prev else None
    )

    # ── Folio count
    total_folios = sum(len(folios) for folios in folios_by_account.values())

    avg_aum_per_client = total_aum / active_clients if active_clients else None

    # ── Top lists
    accounts_list = list(per_account.values())
    top_aum = sorted(
        [b for b in accounts_list if b["current_value"]],
        key=lambda b: -b["current_value"],
    )[:top_n]
    top_gains = sorted(
        [b for b in accounts_list if b["total_gain"]],
        key=lambda b: -b["total_gain"],
    )[:top_n]
    top_redemptions = sorted(
        [b for b in accounts_list if b["redemptions_amount"]],
        key=lambda b: -b["redemptions_amount"],
    )[:top_n]
    highest_gain = max(accounts_list, key=lambda b: b["total_gain"], default=None)
    highest_loss = min(accounts_list, key=lambda b: b["total_gain"], default=None)

    # ── AUM by AMC / Category / Scheme (using current_value)
    aum_by_amc: dict[tuple, float] = defaultdict(float)
    aum_by_category: dict[str, float] = defaultdict(float)
    aum_by_scheme: dict[int, dict] = {}

    for c in per_combo.values():
        if c["current_value"] is None or c["units"] <= 0:
            continue
        aum_by_amc[(c["amc_code"], c["amc_name"])] += c["current_value"]
        cat = c["asset_class"] or "Other"
        aum_by_category[cat] += c["current_value"]
        if c["scheme_id"] not in aum_by_scheme:
            aum_by_scheme[c["scheme_id"]] = {
                "scheme_code": c["scheme_code"],
                "scheme_name": c["scheme_name"],
                "amc_name": c["amc_name"],
                "asset_class": c["asset_class"],
                "aum": 0.0,
                "invested": 0.0,
                "realized": 0.0,
                "unrealized": 0.0,
                "investors": set(),
            }
        sb = aum_by_scheme[c["scheme_id"]]
        sb["aum"] += c["current_value"]
        sb["invested"] += c["cost_basis"]
        sb["unrealized"] += c["current_value"] - c["cost_basis"]
        sb["investors"].add(c["scheme_id"])  # placeholder; fix below
    # Recompute investors per scheme correctly
    for sch_id, _ in aum_by_scheme.items():
        aum_by_scheme[sch_id]["investors"] = {
            acct_id for (acct_id, s_id), c in per_combo.items()
            if s_id == sch_id and c["units"] > 0 and c["current_value"]
        }

    # Per-scheme realized (across all clients including those who exited)
    per_scheme_realized: dict[int, float] = defaultdict(float)
    for (acct_id, sch_id), c in per_combo.items():
        per_scheme_realized[sch_id] += c["realized"]
    for sch_id, sb in aum_by_scheme.items():
        sb["realized"] = per_scheme_realized.get(sch_id, 0.0)

    aum_by_amc_list = sorted(
        [{"amc_code": k[0], "amc_name": k[1], "aum": round(v, 2)} for k, v in aum_by_amc.items()],
        key=lambda x: -x["aum"],
    )
    total_for_pct = sum(v["aum"] for v in aum_by_amc_list) or 1
    for v in aum_by_amc_list:
        v["pct"] = round(v["aum"] / total_for_pct * 100, 2)

    aum_by_category_list = sorted(
        [{"category": k, "aum": round(v, 2)} for k, v in aum_by_category.items()],
        key=lambda x: -x["aum"],
    )
    total_cat = sum(v["aum"] for v in aum_by_category_list) or 1
    for v in aum_by_category_list:
        v["pct"] = round(v["aum"] / total_cat * 100, 2)

    schemes_list_sorted = sorted(
        aum_by_scheme.values(), key=lambda s: -s["aum"]
    )
    aum_by_scheme_list = [
        {**s, "aum": round(s["aum"], 2), "invested": round(s["invested"], 2),
         "unrealized": round(s["unrealized"], 2), "realized": round(s["realized"], 2),
         "investors": len(s["investors"])}
        for s in schemes_list_sorted
    ]

    # Top / underperforming schemes by % return
    perf_schemes = []
    for s in aum_by_scheme.values():
        if s["invested"] and s["invested"] > 0:
            pct = ((s["aum"] - s["invested"]) / s["invested"]) * 100
            perf_schemes.append({
                "scheme_code": s["scheme_code"], "scheme_name": s["scheme_name"],
                "amc_name": s["amc_name"], "investors": len(s["investors"]),
                "aum": round(s["aum"], 2), "invested": round(s["invested"], 2),
                "unrealized_pct": round(pct, 2),
            })
    perf_schemes.sort(key=lambda s: -s["unrealized_pct"])
    top_performing = perf_schemes[:top_n]
    underperforming = sorted(perf_schemes, key=lambda s: s["unrealized_pct"])[:top_n]

    # Average XIRR across active clients
    xirr_values = [b["xirr_pct"] for b in per_account.values() if b["xirr_pct"] is not None]
    avg_xirr = round(sum(xirr_values) / len(xirr_values), 2) if xirr_values else None

    # ── Concentration & cash exposure
    concentrated_clients = []  # >40% of AUM in a single scheme
    high_cash_clients = []     # >70% of AUM in Liquid/Debt
    for acct_id, b in per_account.items():
        if not b["current_value"]:
            continue
        # Find max scheme weight + cash weight
        cash = 0.0
        max_scheme_weight = 0.0
        max_scheme_name = ""
        for (a_id, sch_id), c in per_combo.items():
            if a_id != acct_id or c["current_value"] is None or c["units"] <= 0:
                continue
            w = c["current_value"] / b["current_value"]
            if w > max_scheme_weight:
                max_scheme_weight = w
                max_scheme_name = c["scheme_name"]
            if c["asset_class"] == "Debt":
                cash += c["current_value"]
        if max_scheme_weight > 0.40:
            concentrated_clients.append({
                "name": b["name"], "pan": b["pan"], "aum": b["current_value"],
                "top_scheme": max_scheme_name, "weight_pct": round(max_scheme_weight * 100, 1),
            })
        cash_weight = cash / b["current_value"] if b["current_value"] else 0
        if cash_weight > 0.70:
            high_cash_clients.append({
                "name": b["name"], "pan": b["pan"], "aum": b["current_value"],
                "cash_pct": round(cash_weight * 100, 1),
            })
    concentrated_clients.sort(key=lambda c: -c["weight_pct"])
    high_cash_clients.sort(key=lambda c: -c["cash_pct"])

    # ── Recent transactions (across all clients)
    recent_txns_q = session.execute(
        select(Transaction, Account, Scheme)
        .join(Account, Account.id == Transaction.account_id)
        .join(Scheme, Scheme.id == Transaction.scheme_id)
        .order_by(Transaction.transaction_date.desc(), Transaction.id.desc())
        .limit(20)
    ).all()
    recent_transactions = [
        {
            "date": t.transaction_date,
            "client": acct.name,
            "pan": acct.pan,
            "scheme_code": s.scheme_code,
            "scheme_name": s.name or s.scheme_code,
            "action": t.action,
            "tag": t.action_tag,
            "units": float(t.units),
            "amount": float(t.amount),
        }
        for t, acct, s in recent_txns_q
    ]

    # ── Large transactions (top by amount, last 90 days)
    large_recent = sorted(
        [
            r for r in recent_transactions
            if r["date"] and r["date"] >= today - dt.timedelta(days=90)
        ],
        key=lambda r: -r["amount"],
    )[:10]
    # Redemption alerts: large redemptions in last 90 days
    redemption_alerts_q = session.execute(
        select(Transaction, Account, Scheme)
        .join(Account, Account.id == Transaction.account_id)
        .join(Scheme, Scheme.id == Transaction.scheme_id)
        .where(Transaction.action == "sell")
        .where(Transaction.transaction_date >= today - dt.timedelta(days=90))
        .order_by(Transaction.amount.desc())
        .limit(10)
    ).all()
    redemption_alerts = [
        {
            "date": t.transaction_date,
            "client": acct.name,
            "pan": acct.pan,
            "scheme_code": s.scheme_code,
            "scheme_name": s.name or s.scheme_code,
            "tag": t.action_tag,
            "amount": float(t.amount),
            "units": float(t.units),
        }
        for t, acct, s in redemption_alerts_q
    ]

    # ── Last NAV date (from AMFI feed)
    # The string `max()` would lex-sort dates incorrectly ("31-Oct-2025" > "28-Apr-2026"),
    # so we parse to dt.date first and pick the true latest.
    nav_map = load_nav_map()
    nav_dates_parsed: list[tuple[dt.date, str]] = []
    for r in nav_map["by_code"].values():
        s = r.get("nav_date")
        if not s:
            continue
        try:
            d = dt.datetime.strptime(s, "%d-%b-%Y").date()
            nav_dates_parsed.append((d, s))
        except ValueError:
            continue
    last_nav_date = max(nav_dates_parsed, default=(None, None))[1] if nav_dates_parsed else None

    # ── Trail income estimate (per annum, on current AUM)
    trail_rate = DEFAULT_TRAIL_RATE
    trail_income_annual = round(total_aum * trail_rate, 2) if total_aum else 0.0

    # ── SIP Book aggregates (across all clients)
    active_sips_count = 0
    paused_sips_count = 0
    cancelled_sips_count = 0
    monthly_sip_commitment = 0.0
    sips_ending_soon = []
    stopped_sips = []
    cutoff_90d = today + dt.timedelta(days=90)
    for acct in acct_rows:
        plans = (acct.meta or {}).get("systematic_plans", [])
        for p in plans:
            status = p.get("status")
            tr = p.get("tr_type", "SIP")
            if status == "active":
                active_sips_count += 1
                # Approximate monthly contribution: SIP/STP add to inflow, SWP subtracts
                amt = float(p.get("amount") or 0)
                freq = (p.get("freq") or "monthly").lower()
                multiplier = {
                    "monthly": 1, "quarterly": 1 / 3, "half-yearly": 1 / 6, "yearly": 1 / 12,
                    "weekly": 4, "fortnightly": 2, "daily": 30,
                }.get(freq, 1)
                if tr == "SWP":
                    monthly_sip_commitment -= amt * multiplier
                else:
                    monthly_sip_commitment += amt * multiplier

                # Ending soon (next 90 days)
                end_str = p.get("end_date")
                if end_str:
                    try:
                        end_d = dt.date.fromisoformat(end_str)
                        if today <= end_d <= cutoff_90d:
                            sips_ending_soon.append({
                                "client": acct.name, "pan": acct.pan,
                                "scheme_code": p.get("scheme_code"),
                                "scheme_name": p.get("scheme_name"),
                                "tr_type": tr, "amount": amt,
                                "end_date": end_d,
                            })
                    except ValueError:
                        pass
            elif status == "paused":
                paused_sips_count += 1
            else:
                cancelled_sips_count += 1
                if status == "cancelled":
                    stopped_sips.append({
                        "client": acct.name, "pan": acct.pan,
                        "scheme_code": p.get("scheme_code"),
                        "scheme_name": p.get("scheme_name"),
                        "tr_type": tr, "amount": float(p.get("amount") or 0),
                        "remarks": p.get("remarks"),
                    })
    sips_ending_soon.sort(key=lambda r: r["end_date"])
    monthly_sip_commitment = round(monthly_sip_commitment, 2)

    # ── Net Inflow / Outflow / Net Flow
    net_inflow = total_purchases   # money INTO mutual funds (from investor)
    net_outflow = total_redemptions
    net_flow = net_inflow - net_outflow

    return {
        # Headline
        "total_aum": round(total_aum, 2),
        "active_clients": active_clients,
        "total_clients": total_clients,
        "total_folios": total_folios,
        "avg_aum_per_client": round(avg_aum_per_client, 2) if avg_aum_per_client else None,

        # Growth (deferred — needs historical NAV file)
        "monthly_aum_growth": None,
        "yearly_aum_growth": None,

        # Flows
        "net_inflow": round(net_inflow, 2),
        "net_outflow": round(net_outflow, 2),
        "net_flow": round(net_flow, 2),
        "total_purchases": round(total_purchases, 2),
        "total_redemptions": round(total_redemptions, 2),

        # Portfolio
        "total_invested": round(total_invested, 2),
        "current_portfolio_value": round(total_aum, 2),
        "total_unrealized": round(total_unrealized, 2),
        "total_realized": round(total_realized, 2),
        "stcg": round(total_stcg, 2),
        "ltcg": round(total_ltcg, 2),
        "capital_losses": round(total_capital_losses, 2),

        # Top lists
        "top_clients_by_aum": top_aum,
        "top_clients_by_gains": top_gains,
        "top_clients_by_redemptions": top_redemptions,

        # Client status
        "dormant_clients": dormant_clients,
        "new_clients_count": new_clients,
        "zero_aum_clients_count": zero_aum_clients,
        "retention_rate_pct": round(retention_rate, 2) if retention_rate is not None else None,

        # Allocations
        "aum_by_amc": aum_by_amc_list,
        "aum_by_category": aum_by_category_list,
        "aum_by_scheme": aum_by_scheme_list[:30],   # top 30

        # Performance
        "top_performing_schemes": top_performing,
        "underperforming_schemes": underperforming,
        "avg_xirr_pct": avg_xirr,
        "highest_gain_client": highest_gain,
        "highest_loss_client": highest_loss,

        # Outliers
        "high_cash_clients": high_cash_clients,
        "concentrated_clients": concentrated_clients,

        # Alerts & activity
        "redemption_alerts": redemption_alerts,
        "large_transactions": large_recent,
        "recent_transactions": recent_transactions[:15],
        "last_nav_date": last_nav_date,

        # Revenue
        "trail_rate_pct": round(trail_rate * 100, 2),
        "trail_income_annual": trail_income_annual,

        # Systematic plans (SIP/STP/SWP)
        "active_sips_count": active_sips_count,
        "paused_sips_count": paused_sips_count,
        "cancelled_sips_count": cancelled_sips_count,
        "monthly_sip_commitment": monthly_sip_commitment,
        "annual_sip_commitment": round(monthly_sip_commitment * 12, 2),
        "sips_ending_soon": sips_ending_soon[:10],
        "stopped_sips": stopped_sips[:10],
    }


def _months_between(d1: dt.date, d2: dt.date) -> int:
    """Approximate calendar-month difference between two dates (d2 − d1)."""
    if not d1 or not d2:
        return 0
    return (d2.year - d1.year) * 12 + (d2.month - d1.month) - (
        1 if d2.day < d1.day else 0
    )
