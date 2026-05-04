"""Portfolio computation helpers — holdings, XIRR, asset allocation.

All functions are pure and operate on SQLAlchemy sessions. They do not
depend on the AMFI NAV module — callers pass current_nav in when needed.
"""
from __future__ import annotations

import datetime as dt
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import select

from openreversefeed.db.models import (
    Account,
    Amc,
    Folio,
    Scheme,
    SourceFile,
    Transaction,
)


@dataclass
class HoldingRow:
    scheme_id: int
    scheme_code: str
    scheme_name: str
    isin: str | None
    amc_code: str
    amc_name: str
    folio_number: str
    units_held: Decimal              # remaining units after all sells applied
    avg_cost: Decimal                # weighted-avg cost of remaining units
    invested: Decimal                # cost basis of REMAINING units (not net cash)
    realized_gain: float = 0.0       # profit/loss booked on units already sold
    current_nav: float | None = None
    nav_date: str | None = None
    amfi_matched_name: str | None = None
    current_value: float | None = None
    gain_loss: float | None = None              # unrealized G/L on remaining units
    gain_loss_pct: float | None = None          # unrealized G/L %
    total_gain: float | None = None             # realized + unrealized
    total_gain_pct: float | None = None
    xirr_pct: float | None = None
    first_txn: dt.date | None = None
    last_txn: dt.date | None = None
    # SEBI category from scheme master (if available)
    sebi_category: str | None = None
    asset_class_master: str | None = None
    lock_in: bool = False
    plan_type_display: str | None = None       # Regular / Direct
    option_display: str | None = None           # Growth / IDCW Payout / IDCW Reinvest
    exit_load: str | None = None
    # Cashflow trail used for XIRR computation (date, amount; amount negative = outflow from investor)
    cashflows: list[tuple[dt.date, float]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# XIRR
# ---------------------------------------------------------------------------


def _xnpv(rate: float, cashflows: list[tuple[dt.date, float]]) -> float:
    """Net present value for a list of (date, amount) cashflows at `rate`."""
    if not cashflows:
        return 0.0
    t0 = cashflows[0][0]
    return sum(cf / ((1 + rate) ** ((d - t0).days / 365.0)) for d, cf in cashflows)


def compute_xirr(cashflows: list[tuple[dt.date, float]]) -> float | None:
    """Compute annualised XIRR (as a decimal, e.g. 0.12 = 12%).

    Uses Newton-Raphson with a bracketing fallback (bisection). Returns
    None if the series doesn't converge or is ill-conditioned.

    Sign convention: cashflows out of investor's pocket are NEGATIVE,
    money returned to the investor is POSITIVE. A terminal "current value"
    is positive.
    """
    if len(cashflows) < 2:
        return None
    has_pos = any(cf > 0 for _, cf in cashflows)
    has_neg = any(cf < 0 for _, cf in cashflows)
    if not (has_pos and has_neg):
        return None

    cashflows = sorted(cashflows)

    # Bisection between -0.99 and 10 (-99% to 1000% annualized)
    lo, hi = -0.99, 10.0
    try:
        f_lo = _xnpv(lo, cashflows)
        f_hi = _xnpv(hi, cashflows)
    except OverflowError:
        return None

    if f_lo * f_hi > 0:
        # No sign change in the bracket — can't bracket a root
        return None

    for _ in range(200):
        mid = (lo + hi) / 2
        try:
            f_mid = _xnpv(mid, cashflows)
        except OverflowError:
            return None
        if abs(f_mid) < 1e-7 or abs(hi - lo) < 1e-9:
            return mid
        if f_lo * f_mid < 0:
            hi = f_mid if False else mid  # noqa — keep explicit
            hi = mid
            f_hi = f_mid
        else:
            lo = mid
            f_lo = f_mid
    return (lo + hi) / 2


# ---------------------------------------------------------------------------
# Asset classification from scheme name
# ---------------------------------------------------------------------------

_EQUITY_KEYWORDS = {
    "equity", "flexi", "flexicap", "multicap", "midcap", "smallcap",
    "largecap", "large & mid", "nifty", "sensex", "bluechip", "focused",
    "elss", "tax saver", "nasdaq", "infrastructure", "pharma", "banking",
    "technology", "consumption", "dividend yield", "value", "contra",
    "bschart",
}
_DEBT_KEYWORDS = {
    "debt", "liquid", "overnight", "ultra short", "short duration",
    "money market", "low duration", "gilt", "psu", "corporate bond",
    "credit risk", "banking and psu", "banking & psu", "dynamic bond",
    "long duration", "medium duration", "floater",
}
_HYBRID_KEYWORDS = {
    "hybrid", "arbitrage", "balanced", "equity savings", "multi asset",
    "multi-asset", "asset allocat", "conservative", "aggressive",
}
_COMMODITY_KEYWORDS = {"gold", "silver"}


def classify_asset(scheme_name: str | None) -> str:
    if not scheme_name:
        return "Other"
    n = scheme_name.lower()
    if any(k in n for k in _HYBRID_KEYWORDS):
        return "Hybrid"
    if any(k in n for k in _COMMODITY_KEYWORDS):
        return "Commodity"
    if any(k in n for k in _DEBT_KEYWORDS):
        return "Debt"
    if any(k in n for k in _EQUITY_KEYWORDS):
        return "Equity"
    return "Other"


# ---------------------------------------------------------------------------
# Holdings computation
# ---------------------------------------------------------------------------


def list_clients_summary(session, nav_lookup=None) -> list[dict]:
    """Return one row per client with aggregate stats for the client list page.

    If *nav_lookup* is provided (a callable ``(scheme_code, scheme_name, isin) ->
    (nav, nav_date, matched_name)``), a Current AUM is also computed for each
    client by valuing net units at the latest NAV.
    """
    rows = session.execute(
        select(
            Account.id,
            Account.name,
            Account.pan,
            Account.ownership_type,
        ).order_by(Account.name)
    ).all()

    # Pre-load all transactions in one query; group in Python for efficiency.
    all_txns = session.execute(
        select(Transaction, Scheme)
        .join(Scheme, Scheme.id == Transaction.scheme_id)
    ).all()

    # Group by account_id → then by scheme_id to compute net units
    from collections import defaultdict
    by_account: dict = defaultdict(list)  # account_id -> list of (txn, scheme)
    for t, s in all_txns:
        by_account[t.account_id].append((t, s))

    out = []
    for account_id, name, pan, ownership in rows:
        entries = by_account.get(account_id, [])
        if not entries:
            out.append({
                "account_id": str(account_id),
                "name": name,
                "pan": pan,
                "ownership": ownership or "—",
                "folios": 0,
                "schemes": 0,
                "txn_count": 0,
                "invested": 0.0,
                "current_aum": None,
                "aum_coverage_pct": None,
                "first_txn": None,
                "last_txn": None,
            })
            continue

        folios = {t.folio_id for t, _ in entries}
        schemes = {t.scheme_id for t, _ in entries}
        dates = [t.transaction_date for t, _ in entries if t.transaction_date]

        # Time-ordered weighted-avg cost accounting per (scheme) — same logic
        # as compute_client_holdings. Gives us, per scheme:
        #  - remaining_units, remaining_cost_basis (= "invested")
        #  - realized_gain (cumulative booked profit across sold units)
        entries_by_scheme: dict = defaultdict(list)
        scheme_obj: dict = {}
        for t, s in entries:
            entries_by_scheme[s.id].append(t)
            scheme_obj[s.id] = s

        total_cost_basis = 0.0       # sum of remaining cost basis across all schemes
        total_realized = 0.0         # sum of realized gains across all schemes
        total_current_value = 0.0    # sum of current value at live NAV
        priced_cost = 0.0            # cost basis of priced holdings (for coverage)
        any_priced = False

        for sid, sch_txns in entries_by_scheme.items():
            s = scheme_obj[sid]
            sch_txns = sorted(
                sch_txns, key=lambda t: (t.transaction_date or dt.date.min, t.id)
            )
            remaining_units = Decimal("0")
            remaining_cost = Decimal("0")
            realized = Decimal("0")
            for t in sch_txns:
                if t.action == "buy":
                    remaining_units += t.units
                    remaining_cost += t.amount
                elif t.action == "sell":
                    avg_cost_at_sale = (
                        remaining_cost / remaining_units if remaining_units > 0 else Decimal("0")
                    )
                    units_with_cost = (
                        min(t.units, remaining_units) if remaining_units > 0 else Decimal("0")
                    )
                    cost_of_sold = units_with_cost * avg_cost_at_sale
                    realized += t.amount - cost_of_sold
                    remaining_units -= t.units
                    remaining_cost -= cost_of_sold
                    if remaining_units < 0:
                        remaining_units = Decimal("0")
                        remaining_cost = Decimal("0")

            if remaining_units <= Decimal("0"):
                remaining_units = Decimal("0")
                remaining_cost = Decimal("0")

            total_cost_basis += float(remaining_cost)
            total_realized += float(realized)

            if nav_lookup is not None and remaining_units > 0:
                nav, _, _ = nav_lookup(s.scheme_code, s.name or s.scheme_code, s.isin)
                if nav:
                    total_current_value += float(remaining_units) * nav
                    priced_cost += float(remaining_cost)
                    any_priced = True

        invested = round(total_cost_basis, 2)  # "Net Invested" = cost basis of remaining
        realized = round(total_realized, 2)
        current_aum = round(total_current_value, 2) if any_priced and total_current_value else None
        aum_coverage = None
        if total_cost_basis > 0 and priced_cost >= 0 and any_priced:
            aum_coverage = round(priced_cost / total_cost_basis * 100, 1)

        # P&L components: unrealized (priced only) + realized
        unrealized = None
        pnl = None
        pnl_pct = None
        if current_aum is not None:
            unrealized = round(current_aum - priced_cost, 2)
            pnl = round(unrealized + realized, 2)
            basis_for_pct = priced_cost + max(realized, 0)
            if basis_for_pct > 0:
                pnl_pct = round(pnl / basis_for_pct * 100, 2)
        elif realized:
            pnl = realized  # fully exited positions with no live holdings

        out.append({
            "account_id": str(account_id),
            "name": name,
            "pan": pan,
            "ownership": ownership or "—",
            "folios": len(folios),
            "schemes": len(schemes),
            "txn_count": len(entries),
            "invested": invested,
            "realized": realized,
            "unrealized": unrealized,
            "current_aum": current_aum,
            "aum_coverage_pct": aum_coverage,
            "pnl": pnl,
            "pnl_pct": pnl_pct,
            "first_txn": min(dates) if dates else None,
            "last_txn": max(dates) if dates else None,
        })

    return out


def get_client(session, pan: str):
    """Return the Account row (first one matching PAN), or None."""
    return session.execute(
        select(Account).where(Account.pan == pan)
    ).scalars().first()


def get_client_folios(session, account_id) -> list[dict]:
    rows = session.execute(
        select(Folio, Amc)
        .join(Amc, Amc.id == Folio.amc_id)
        .where(Folio.account_id == account_id)
        .order_by(Amc.code, Folio.folio_number)
    ).all()
    return [
        {
            "folio_id": f.id,
            "folio_number": f.folio_number,
            "amc_code": a.code,
            "amc_name": a.name,
        }
        for f, a in rows
    ]


def compute_client_holdings(session, account_id) -> list[HoldingRow]:
    """One holding row per (folio, scheme) combination for this client."""
    rows = session.execute(
        select(Transaction, Scheme, Folio, Amc)
        .join(Scheme, Scheme.id == Transaction.scheme_id)
        .join(Folio, Folio.id == Transaction.folio_id)
        .join(Amc, Amc.id == Scheme.amc_id)
        .where(Transaction.account_id == account_id)
        .order_by(Transaction.transaction_date, Transaction.id)
    ).all()

    # Group by (folio_id, scheme_id)
    groups: dict[tuple, list] = defaultdict(list)
    for t, s, f, a in rows:
        groups[(f.id, s.id)].append((t, s, f, a))

    out: list[HoldingRow] = []
    for (_folio_id, _scheme_id), txns in groups.items():
        # Time-ordered weighted-average cost accounting.
        # Walk transactions in chronological order:
        #  - BUY:  add to remaining units and remaining cost basis
        #  - SELL: compute realized gain = sell_amount − (sold_units × avg_cost),
        #          remove units and proportional cost basis.
        # At the end:
        #  - units_held = remaining units
        #  - avg_cost   = weighted-avg cost of remaining units
        #  - invested   = cost basis of REMAINING units (NOT net cash flow)
        #  - realized_gain = running sum of profit/loss booked on sold units
        remaining_units = Decimal("0")
        remaining_cost = Decimal("0")
        realized_gain = Decimal("0")
        dates: list[dt.date] = []
        cashflows: list[tuple[dt.date, float]] = []

        # Sort by (date, id) for stable chronological processing
        txns_sorted = sorted(
            txns, key=lambda row: (row[0].transaction_date or dt.date.min, row[0].id)
        )

        for t, s, f, a in txns_sorted:
            if t.transaction_date:
                dates.append(t.transaction_date)

            if t.action == "buy":
                remaining_units += t.units
                remaining_cost += t.amount
                cashflows.append((t.transaction_date, -float(t.amount)))
            elif t.action == "sell":
                cashflows.append((t.transaction_date, float(t.amount)))

                if remaining_units > 0:
                    avg_cost_at_sale = remaining_cost / remaining_units
                else:
                    # Overselling edge case — treat unseen cost basis as 0.
                    avg_cost_at_sale = Decimal("0")

                # How many of the sold units have a known cost basis
                units_with_cost = min(t.units, remaining_units) if remaining_units > 0 else Decimal("0")
                cost_of_sold = units_with_cost * avg_cost_at_sale
                realized_gain += t.amount - cost_of_sold

                remaining_units -= t.units
                remaining_cost -= cost_of_sold
                if remaining_units < 0:
                    # Overselling: clamp so reporting stays clean.
                    remaining_units = Decimal("0")
                    remaining_cost = Decimal("0")

        # Guard against negative tiny dust from decimal rounding.
        if remaining_units <= Decimal("0"):
            remaining_units = Decimal("0")
            remaining_cost = Decimal("0")

        net_units = remaining_units
        net_invested = remaining_cost  # cost basis of remaining (money still at work)
        avg_cost = (
            (remaining_cost / remaining_units) if remaining_units > 0 else Decimal("0")
        )

        _, s0, f0, a0 = txns[0]
        meta = s0.meta or {}

        # Humanize the DB's plan_type / option into display strings.
        # Schema: DB.option ∈ {direct, regular}; DB.plan_type ∈ {growth, idcw_payout, idcw_reinvest}
        _plan_map = {"direct": "Direct", "regular": "Regular"}
        _option_map = {
            "growth": "Growth",
            "idcw_payout": "IDCW Payout",
            "idcw_reinvest": "IDCW Reinvest",
        }
        plan_disp = _plan_map.get((s0.option or "").lower())
        opt_disp = _option_map.get((s0.plan_type or "").lower())

        out.append(
            HoldingRow(
                scheme_id=s0.id,
                scheme_code=s0.scheme_code,
                scheme_name=s0.name or s0.scheme_code,
                isin=s0.isin,
                amc_code=a0.code,
                amc_name=a0.name,
                folio_number=f0.folio_number,
                units_held=net_units,
                avg_cost=avg_cost,
                invested=net_invested,
                realized_gain=float(realized_gain),
                sebi_category=meta.get("nature"),
                asset_class_master=meta.get("asset_class"),
                lock_in=bool(meta.get("lock_in")),
                plan_type_display=plan_disp,
                option_display=opt_disp,
                exit_load=meta.get("exit_load"),
                first_txn=min(dates) if dates else None,
                last_txn=max(dates) if dates else None,
                cashflows=cashflows,
            )
        )

    return out


def enrich_with_current_value(
    holdings: list[HoldingRow], nav_lookup
) -> list[HoldingRow]:
    """Fill in current_nav, current_value, unrealized gain_loss, total_gain, xirr.

    For holdings with units_held == 0 (fully exited), there is no current_value
    and no unrealized gain — total_gain equals realized_gain.
    """
    today = dt.date.today()
    for h in holdings:
        nav, nav_date, matched = nav_lookup(h.scheme_code, h.scheme_name, h.isin)
        h.current_nav = nav
        h.nav_date = nav_date
        h.amfi_matched_name = matched

        if h.units_held > 0 and nav is not None:
            # Active holding with live NAV
            current_value = float(h.units_held) * nav
            h.current_value = round(current_value, 2)

            # Unrealized gain on the units still held
            unrealized = current_value - float(h.invested)
            h.gain_loss = round(unrealized, 2)
            if float(h.invested) > 0:
                h.gain_loss_pct = round((unrealized / float(h.invested)) * 100, 2)

            # Total gain = realized (from past sells) + unrealized (on current holdings)
            total = unrealized + h.realized_gain
            h.total_gain = round(total, 2)
            # Total-gain % is against the original cost ever invested (realized cost + remaining cost)
            basis = float(h.invested) + max(h.realized_gain, 0)  # conservative denominator
            if basis > 0:
                h.total_gain_pct = round((total / basis) * 100, 2)

            xirr_flows = list(h.cashflows) + [(today, current_value)]
            xirr = compute_xirr(xirr_flows)
            if xirr is not None:
                h.xirr_pct = round(xirr * 100, 2)
        elif h.units_held <= 0:
            # Fully exited position — no current_value. Realized gain is the only gain.
            h.current_value = 0.0
            h.gain_loss = 0.0  # no unrealized gain without holdings
            h.gain_loss_pct = None
            h.total_gain = round(h.realized_gain, 2)
            # XIRR only if we have both sides
            xirr = compute_xirr(h.cashflows) if len(h.cashflows) >= 2 else None
            if xirr is not None:
                h.xirr_pct = round(xirr * 100, 2)

    return holdings


def compute_portfolio_summary(holdings: list[HoldingRow]) -> dict:
    """Aggregate per-holding values into a portfolio summary.

    "Invested Value" = cost basis of units STILL HELD (not net cash).
    "Unrealized Gain" = sum of current_value − invested (for live holdings).
    "Realized Gain"   = sum of gains booked on sold units across all schemes.
    "Total Gain"      = realized + unrealized.
    """
    total_invested = sum(float(h.invested) for h in holdings)
    total_current = sum(h.current_value for h in holdings if h.current_value)
    total_realized = sum(h.realized_gain for h in holdings)

    # Unrealized = sum of per-holding unrealized G/L (only for priced holdings)
    total_unrealized = sum(
        h.gain_loss for h in holdings if h.gain_loss is not None and h.units_held > 0
    )
    # If no holdings were priced, fall back to current-minus-invested arithmetic
    if total_current and total_unrealized == 0 and any(h.units_held > 0 for h in holdings):
        total_unrealized = total_current - total_invested

    total_gain = total_realized + total_unrealized

    # Asset allocation — two views:
    #  (a) by current value (only holdings with a live NAV; adds to total_current)
    #  (b) by invested amount (all holdings; adds to total_invested)
    # Mixing the two bases creates apples-to-oranges totals, so we keep them
    # separate and let the UI pick which to show.
    alloc_current: dict[str, float] = defaultdict(float)
    alloc_invested: dict[str, float] = defaultdict(float)
    for h in holdings:
        cat = classify_asset(h.scheme_name)
        if float(h.invested) > 0:
            alloc_invested[cat] += float(h.invested)
        if h.current_value and h.current_value > 0:
            alloc_current[cat] += h.current_value

    if alloc_current:
        total_basis = sum(alloc_current.values()) or 1.0
        allocation = [
            {
                "category": cat,
                "value": round(v, 2),
                "pct": round(v / total_basis * 100, 2),
            }
            for cat, v in sorted(alloc_current.items(), key=lambda kv: -kv[1])
        ]
        allocation_basis = "current_value"
    else:
        total_basis = sum(alloc_invested.values()) or 1.0
        allocation = [
            {
                "category": cat,
                "value": round(v, 2),
                "pct": round(v / total_basis * 100, 2),
            }
            for cat, v in sorted(alloc_invested.items(), key=lambda kv: -kv[1])
        ]
        allocation_basis = "invested"

    # Portfolio-level XIRR: combine all cashflows + total current value
    today = dt.date.today()
    all_flows: list[tuple[dt.date, float]] = []
    for h in holdings:
        all_flows.extend(h.cashflows)
    if total_current and total_current > 0:
        all_flows.append((today, total_current))
    xirr = compute_xirr(all_flows) if len(all_flows) >= 2 else None

    # Denominator for %-change: remaining cost + total cost that was already sold.
    # Approximated as invested + realized(positive) to avoid divide-by-zero on full exits.
    basis_for_pct = total_invested + max(total_realized, 0)

    return {
        "total_invested": round(total_invested, 2),
        "total_current": round(total_current, 2) if total_current else None,
        "total_realized": round(total_realized, 2),
        "total_unrealized": round(total_unrealized, 2) if total_current else None,
        "total_gain": round(total_gain, 2) if (total_current or total_realized) else None,
        "total_gain_pct": (
            round(total_gain / basis_for_pct * 100, 2) if basis_for_pct > 0 else None
        ),
        "xirr_pct": round(xirr * 100, 2) if xirr is not None else None,
        "allocation": allocation,
        "allocation_basis": allocation_basis,
        "allocation_total": round(sum(a["value"] for a in allocation), 2),
        "num_holdings": len([h for h in holdings if h.units_held > 0]),
        "num_folios": len({h.folio_number for h in holdings}),
    }


def list_all_client_transactions(session, account_id) -> list[dict]:
    """Return ALL transactions for a client across all schemes, with tag info."""
    rows = session.execute(
        select(Transaction, Scheme, Folio, Amc, SourceFile)
        .join(Scheme, Scheme.id == Transaction.scheme_id)
        .join(Folio, Folio.id == Transaction.folio_id)
        .join(Amc, Amc.id == Scheme.amc_id)
        .join(SourceFile, SourceFile.id == Transaction.source_file_id)
        .where(Transaction.account_id == account_id)
        .order_by(Transaction.transaction_date.desc(), Transaction.id.desc())
    ).all()
    return [
        {
            "id": t.id,
            "date": t.transaction_date,
            "scheme_code": s.scheme_code,
            "scheme_name": s.name or s.scheme_code,
            "folio_number": f.folio_number,
            "amc_code": a.code,
            "action": t.action,
            "tag": t.action_tag,
            "units": float(t.units),
            "amount": float(t.amount),
            "nav": float(t.nav) if t.nav else None,
            "reference": t.registrar_transaction_id,
            "txn_number": t.registrar_transaction_number,
            "registrar": t.registrar,
            "source_file": sf.filename,
        }
        for t, s, f, a, sf in rows
    ]


def list_holding_transactions(session, account_id, scheme_id, folio_id=None) -> list[dict]:
    """Return raw transactions for a single scheme holding (optionally a specific folio)."""
    q = (
        select(Transaction, SourceFile)
        .join(SourceFile, SourceFile.id == Transaction.source_file_id)
        .where(Transaction.account_id == account_id)
        .where(Transaction.scheme_id == scheme_id)
        .order_by(Transaction.transaction_date, Transaction.id)
    )
    if folio_id is not None:
        q = q.where(Transaction.folio_id == folio_id)

    rows = session.execute(q).all()
    return [
        {
            "id": t.id,
            "date": t.transaction_date,
            "action": t.action,
            "tag": t.action_tag,
            "units": float(t.units),
            "amount": float(t.amount),
            "nav": float(t.nav) if t.nav else None,
            "reference": t.registrar_transaction_id,
            "txn_number": t.registrar_transaction_number,
            "registrar": t.registrar,
            "source_file": sf.filename,
        }
        for t, sf in rows
    ]
