"""Benchmark comparison — picks a Nifty index proxy for any given fund.

Why proxy index funds?
  Direct Nifty values are gated behind nseindia.com which is hostile to
  programmatic access. Index funds that track each Nifty index replicate
  it to within ~0.05% (their tracking error) — close enough for a visual
  benchmark comparison, and reachable through the same mfapi.in pipeline
  we already use for fund NAV history.

Mapping (per the user's spec):
    largecap → Nifty 50
    midcap   → Nifty Midcap 150
    smallcap → Nifty Smallcap 250
    default  → Nifty 500   (used for multi-cap / flexi / ELSS / sectoral
                            and any other equity scheme that isn't a pure
                            cap-segment fund)
    debt / hybrid / commodity → no benchmark shown (would mislead)
"""
from __future__ import annotations

from typing import Any


# Each entry: which Nifty we're showing, what scheme on mfapi.in we're using
# as the proxy, and a short note for the UI tooltip / data-source label.
BENCHMARKS: dict[str, dict[str, str]] = {
    "largecap": {
        "key": "largecap",
        "label": "Nifty 50",
        "amfi_code": "120586",
        "tracker": "UTI Nifty 50 Index Fund — Direct Growth",
    },
    "midcap": {
        "key": "midcap",
        "label": "Nifty Midcap 150",
        "amfi_code": "144627",
        "tracker": "Motilal Oswal Nifty Midcap 150 Index Fund — Direct Growth",
    },
    "smallcap": {
        "key": "smallcap",
        "label": "Nifty Smallcap 250",
        "amfi_code": "147622",
        "tracker": "Motilal Oswal Nifty Smallcap 250 Index Fund — Direct Growth",
    },
    "default": {
        "key": "default",
        "label": "Nifty 500",
        "amfi_code": "147625",
        "tracker": "Motilal Oswal Nifty 500 Index Fund — Direct Growth",
    },
}


_NON_EQUITY_HINTS = (
    "debt", "liquid", "money market", "gilt", "corporate bond", "credit risk",
    "ultra short", "low duration", "short duration", "medium duration",
    "long duration", "dynamic bond", "banking and psu", "psu debt",
    "overnight", "fixed maturity",
    "gold", "silver", "commodity",
)


def pick_benchmark(scheme_name: str, meta: dict[str, Any]) -> dict[str, str] | None:
    """Return the benchmark dict for this scheme, or None if not applicable.

    Decision tree:
      1. If the SEBI category / scheme name signals non-equity → no benchmark.
      2. Mid-cap, small-cap, or large-cap match → that bucket.
      3. Anything else equity-flavoured → Nifty 500 (default).
      4. Couldn't classify confidently → no benchmark (conservative).
    """
    sebi = (meta.get("sebi_category") or "").lower()
    asset_class = (meta.get("asset_class") or "").lower()
    name = (scheme_name or "").lower()
    haystack = f"{name} | {sebi} | {asset_class}"

    # 1. Non-equity exclusions — pure debt / commodity / gold funds.
    # (Arbitrage, balanced advantage, equity-savings and conservative hybrid
    # funds are kept in scope: their NAV growth IS comparable to broad equity
    # for the user-facing "did the fund beat the market?" question, even if
    # they're not a perfect peer benchmark in academic terms.)
    for hint in _NON_EQUITY_HINTS:
        if hint in haystack:
            return None

    # 2. Multi-segment funds → fall through to Nifty 500 default below
    is_large_and_mid = any(k in haystack for k in ("large & mid", "large and mid"))

    # 3. Specific cap-segment matches (most specific wins; check small before
    # mid because "small cap" never contains "mid", and skip multi-segment).
    if not is_large_and_mid:
        if any(k in haystack for k in ("small cap", "smallcap", "small-cap")):
            return BENCHMARKS["smallcap"]
        if any(k in haystack for k in ("mid cap", "midcap", "mid-cap")):
            return BENCHMARKS["midcap"]
        if any(k in haystack for k in ("large cap", "largecap", "large-cap")):
            return BENCHMARKS["largecap"]

    # 3. Generic equity / hybrid → Nifty 500 default. Hybrid funds usually
    # carry 30-70% equity exposure; the Nifty 500 line gives the advisor a
    # quick "did this still keep up with broad market?" read even though it's
    # not a perfect peer benchmark.
    if "equity" in asset_class or "hybrid" in asset_class or "equity" in sebi or any(
        k in haystack for k in (
            "flexi cap", "multi cap", "elss", "tax saver", "value",
            "contra", "focused", "sectoral", "thematic", "dividend yield",
            "nifty", "sensex", "bse", "index fund",
            "hybrid", "balanced advantage", "arbitrage", "equity savings",
            "conservative hybrid", "balanced",
        )
    ):
        return BENCHMARKS["default"]

    # 4. Couldn't classify — skip benchmark rather than mislead
    return None


def normalize_to_base(series: list[dict[str, Any]], base: float = 100.0) -> list[dict[str, Any]]:
    """Re-base a NAV series so the first point = ``base``. Lets us overlay
    two series with very different absolute NAVs on the same chart axis."""
    if not series:
        return []
    start = series[0]["nav"]
    if not start or start <= 0:
        return []
    factor = base / start
    return [{"date": r["date"], "nav": round(r["nav"] * factor, 4)} for r in series]


def align_series(fund_series: list[dict], bench_series: list[dict]) -> tuple[list[dict], list[dict]]:
    """Trim both series to their common date range so the chart starts/ends
    at the same point on each line."""
    if not fund_series or not bench_series:
        return fund_series, bench_series
    fund_dates = {r["date"] for r in fund_series}
    bench_dates = {r["date"] for r in bench_series}
    common = fund_dates & bench_dates
    if not common:
        return fund_series, bench_series
    f = [r for r in fund_series if r["date"] in common]
    b = [r for r in bench_series if r["date"] in common]
    return f, b
