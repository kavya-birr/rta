"""CAMS Scheme Master (R39) ingester.

Filename pattern: ``*R39.dbf`` — sometimes named with a SIP-sounding prefix
because it carries SIP rules per scheme, but it's actually a SCHEME MASTER
(rules for SIP/SWP/STP/Switch per scheme, SEBI classification, ELSS flags,
plan type, ISINs). It is *not* per-client SIP registration data.

What we extract:
  * ISIN — match to our existing schemes by ISIN, fill where missing
  * SEBI classification (canonical SEBI category)
  * ELSS lock-in flag (-> 3-year lock)
  * Asset class (EQUITY / DEBT / HYBRID etc.)
  * Plan type (REGULAR / DIRECT)
  * SIP rules (allowed / min amount / valid dates / frequencies / min installments)
  * SWP / STP rules (allowed flags + min amounts)
  * Settlement period (T+N days)

The transaction-feed scheme code is ``AMC_CODE + SCH_CODE`` (e.g. CAMS feed has
``B43N`` while the master has AMC=B, SCH_CODE=43N). We use both ISIN match
(primary) and the concatenated code as a fallback.
"""
from __future__ import annotations

import math
from typing import Any

import pandas as pd
from sqlalchemy import select

from openreversefeed.db.models import Amc, Scheme

# Headers unique to this file (used by the routing layer to detect)
_CAMS_R39_REQUIRED = {
    "AMC_CODE", "AMC", "SCH_CODE", "SCH_NAME", "ISIN_NO",
    "SIP_ALLOW", "SEBI_CLASS", "ASSET_CLAS", "ELSS_SCH",
}


def is_cams_scheme_master(headers: set[str]) -> bool:
    """True if the column set looks like a CAMS R39 scheme master file."""
    return _CAMS_R39_REQUIRED.issubset({h.strip() for h in headers})


def _clean(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and math.isnan(v):
        return ""
    s = str(v).strip()
    if s.lower() == "nan":
        return ""
    return s


def _clean_num(v: Any) -> float | None:
    if v is None:
        return None
    if isinstance(v, float) and math.isnan(v):
        return None
    s = str(v).strip()
    if not s or s.lower() == "nan":
        return None
    try:
        f = float(s)
        if math.isnan(f) or math.isinf(f):
            return None
        return f
    except ValueError:
        return None


def _map_plan_type(p: str) -> str | None:
    p = p.upper()
    if p == "REGULAR":
        return "regular"
    if p == "DIRECT":
        return "direct"
    return None


def _map_asset_class(asset_clas: str, sebi_class: str, sch_type: str) -> str:
    """Map CAMS' ASSET_CLAS to our display labels."""
    a = (asset_clas or "").upper()
    s = (sebi_class or "").lower()
    t = (sch_type or "").lower()

    if a == "EQUITY" or "equity" in s:
        return "Equity"
    if a == "DEBT" or "debt" in s:
        return "Debt"
    if "hybrid" in s or "balanc" in s:
        return "Hybrid"
    if "gold" in t or "silver" in t or "commodity" in s:
        return "Commodity"
    if "liquid" in t:
        return "Debt"
    return asset_clas.title() if asset_clas else "Other"


def self_apply_master_to_scheme(
    scheme: Scheme, isin: str, sch_name: str, plan_type: str,
    sebi_class: str, asset_clas: str, sch_type: str, elss: bool,
    row: pd.Series, stats: dict,
) -> None:
    """Apply master-file fields to a single Scheme row."""
    changed = False
    if isin and isin != "-" and not scheme.isin:
        scheme.isin = isin
        stats["isins_filled"] += 1
        changed = True
    if sch_name and scheme.name == scheme.scheme_code:
        scheme.name = sch_name
        changed = True
    db_option = _map_plan_type(plan_type)
    if db_option and not scheme.option:
        scheme.option = db_option
        changed = True

    meta = dict(scheme.meta or {})
    new_asset = _map_asset_class(asset_clas, sebi_class, sch_type)
    if meta.get("asset_class") != new_asset:
        meta["asset_class"] = new_asset
        changed = True
    if sebi_class and meta.get("sebi_category") != sebi_class:
        meta["sebi_category"] = sebi_class
        stats["sebi_categories_set"] += 1
        changed = True
    new_nature = sebi_class or sch_type
    if new_nature and meta.get("nature") != new_nature:
        meta["nature"] = new_nature
        changed = True
    if elss and not meta.get("lock_in"):
        meta["lock_in"] = True
        meta["lock_in_period_days"] = 36 * 30
        stats["elss_flags_set"] += 1
        changed = True
    if plan_type:
        meta["plan_type_full"] = plan_type
    sip_min = _clean_num(row.get("SIP_MN_AMT"))
    if sip_min is not None:
        meta["sip_min_amount"] = sip_min
    sip_dates = _clean(row.get("SIP_DATES"))
    if sip_dates:
        meta["sip_dates"] = sip_dates
    sip_freqs = _clean(row.get("SYS_FREQS"))
    if sip_freqs:
        meta["sys_frequencies"] = sip_freqs
    if _clean(row.get("SIP_ALLOW")) == "Y":
        meta["sip_allowed"] = True
    if _clean(row.get("SWP_ALLOW")) == "Y":
        meta["swp_allowed"] = True
    if _clean(row.get("STP_ALLOW")) in ("Y", "B", "I", "O"):
        meta["stp_allowed"] = True
    settle = _clean_num(row.get("SETTLE_PER"))
    if settle is not None:
        meta["red_settle_days"] = settle
    meta.setdefault("source", "cams_r39")

    if meta != (scheme.meta or {}):
        scheme.meta = meta
        changed = True
    if changed:
        stats["schemes_updated"] += 1


def ingest_cams_scheme_master(session, raw_df: pd.DataFrame) -> dict[str, Any]:
    """Sync CAMS scheme master into Scheme table + meta.

    Match strategy:
      1. By ISIN_NO (primary, most reliable)
      2. By concatenated AMC_CODE + SCH_CODE matching our scheme_code
    """
    stats = {
        "rows_in": int(len(raw_df)),
        "matched_by_isin": 0,
        "matched_by_code": 0,
        "schemes_updated": 0,
        "schemes_created": 0,
        "isins_filled": 0,
        "elss_flags_set": 0,
        "sebi_categories_set": 0,
    }

    # Pre-load existing schemes. Multiple schemes can share an ISIN because
    # earlier ingests (WBR39A) created parallel master rows. We update ALL
    # schemes that share the matched ISIN so transaction-linked rows get
    # the metadata too.
    existing_schemes = session.execute(select(Scheme)).scalars().all()
    by_isin: dict[str, list[Scheme]] = {}
    for s in existing_schemes:
        if s.isin:
            by_isin.setdefault(s.isin, []).append(s)
    by_code: dict[str, Scheme] = {s.scheme_code: s for s in existing_schemes}

    # Pre-load AMCs to attach new schemes properly (or use existing AMC by code)
    amcs_by_code: dict[str, Amc] = {
        a.code: a for a in session.execute(select(Amc)).scalars().all()
    }

    for _, row in raw_df.iterrows():
        amc_code = _clean(row.get("AMC_CODE"))
        amc_name = _clean(row.get("AMC"))
        master_sch = _clean(row.get("SCH_CODE"))
        sch_name = _clean(row.get("SCH_NAME"))
        isin = _clean(row.get("ISIN_NO"))
        sebi_class = _clean(row.get("SEBI_CLASS"))
        asset_clas = _clean(row.get("ASSET_CLAS"))
        sch_type = _clean(row.get("SCH_TYPE"))
        plan_type = _clean(row.get("PLAN_TYPE"))
        elss = _clean(row.get("ELSS_SCH")) == "Y"

        if not isin and not master_sch:
            continue

        # ─── Match existing schemes (multiple may share an ISIN) ───
        targets: list[Scheme] = []
        if isin and isin != "-" and isin in by_isin:
            targets = list(by_isin[isin])
            stats["matched_by_isin"] += 1
        else:
            # Try AMC_CODE + master SCH_CODE concatenation (e.g. "B"+"43N" = "B43N")
            concat_code = amc_code + master_sch
            if concat_code in by_code:
                targets = [by_code[concat_code]]
                stats["matched_by_code"] += 1

        if not targets:
            # We don't auto-create — only update existing schemes that clients hold
            continue

        for scheme in targets:
            self_apply_master_to_scheme(
                scheme, isin, sch_name, plan_type, sebi_class, asset_clas,
                sch_type, elss, row, stats,
            )

    session.commit()
    return stats
