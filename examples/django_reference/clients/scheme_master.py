"""KFintech WBR39A scheme-master ingester.

The WBR39A file is a catalog of every mutual fund scheme KFintech tracks, with
AMC codes/names, ISINs, SEBI categories, lock-in rules, SIP/SWP parameters, etc.
It is NOT a transaction feed — rows map to schemes, not transactions.

This module syncs that master data into the existing `amcs` and `schemes`
tables (updating names, ISINs, and stashing the rich metadata in `schemes.meta`).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import select

from openreversefeed.db.models import Amc, Scheme

# Header signature used for WBR39A detection. The file has 98 columns; we
# pick a small set of distinctive column names so we can identify it without
# being fragile to optional columns.
_WBR39A_REQUIRED = {"AMC_CODE", "AMC_NAME", "ISIN_NO", "SCHEME_COD", "SCHEME_NAM", "NATURE", "ASSET_CLAS"}


def is_scheme_master(headers: set[str]) -> bool:
    """Return True if the header set looks like a KFintech WBR39A scheme master."""
    return _WBR39A_REQUIRED.issubset({h.strip() for h in headers})


import math


def _clean_str(v: Any) -> str:
    if v is None:
        return ""
    # pandas may produce float('nan') for empty cells
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


def _derive_option_from_name(scheme_name: str, plan_type_field: str) -> str | None:
    """DB.option ∈ {direct, regular} — derived from scheme name / PLAN_TYPE field."""
    n = (scheme_name or "").lower()
    p = (plan_type_field or "").strip().upper()
    if "direct" in n or p == "DIRECT":
        return "direct"
    if "regular" in n or p == "REGULAR":
        return "regular"
    return None


def _derive_plan_type_from_name(scheme_name: str) -> str | None:
    """DB.plan_type ∈ {growth, idcw_payout, idcw_reinvest} — derived from scheme name."""
    n = (scheme_name or "").lower()
    if not n:
        return None
    # Reinvest variants must be detected before payout (to avoid "idcw payout reinvest" ambiguity)
    if "reinvest" in n:
        return "idcw_reinvest"
    if "growth" in n:
        return "growth"
    if "idcw" in n or "dividend" in n or "payout" in n:
        return "idcw_payout"
    return None


def ingest_scheme_master(session, raw_df) -> dict[str, Any]:
    """Sync AMC + Scheme master data from a WBR39A DataFrame.

    Behaviour:
      - AMC: upsert by AMC_CODE. If name is blank/placeholder, replace with AMC_NAME.
      - Scheme: upsert by (scheme_code, plan_type, option_code). Fills in name,
        ISIN, meta (nature, asset_class, lock_in, SIP/SWP/STP parameters).
      - Never overwrites user-added data with blanks.
    """
    stats = {
        "rows_in": int(len(raw_df)),
        "amcs_created": 0,
        "amcs_updated": 0,
        "schemes_created": 0,
        "schemes_updated": 0,
        "skipped": 0,
    }

    amc_cache: dict[str, Amc] = {}

    for _, row in raw_df.iterrows():
        amc_code = _clean_str(row.get("AMC_CODE"))
        amc_name = _clean_str(row.get("AMC_NAME"))
        scheme_code = _clean_str(row.get("SCHEME_COD"))
        scheme_name = _clean_str(row.get("SCHEME_NAM"))
        isin = _clean_str(row.get("ISIN_NO"))
        plan_type = _clean_str(row.get("PLAN_TYPE"))
        option_code = _clean_str(row.get("OPTIONCODE"))
        option_desc = _clean_str(row.get("OPTION_DES"))

        if not amc_code or not scheme_code:
            stats["skipped"] += 1
            continue

        # --- AMC upsert ---
        amc = amc_cache.get(amc_code)
        if amc is None:
            amc = session.execute(
                select(Amc).where(Amc.code == amc_code)
            ).scalars().first()
            if amc is None:
                amc = Amc(code=amc_code, name=amc_name or amc_code, meta={})
                session.add(amc)
                session.flush()
                stats["amcs_created"] += 1
            elif amc_name and (amc.name == amc.code or amc.name.startswith("AMC ")):
                amc.name = amc_name
                stats["amcs_updated"] += 1
            amc_cache[amc_code] = amc

        # --- Scheme upsert (lookup deferred below after plan_type/option are mapped) ---
        meta_update = {
            "nature": _clean_str(row.get("NATURE")) or None,
            "asset_class": _clean_str(row.get("ASSET_CLAS")) or None,
            "sub_asset": _clean_str(row.get("SUB_ASSET")) or None,
            "plan_type_full": plan_type or None,
            "option_description": option_desc or None,
            "face_value": _clean_num(row.get("FACE_VALUE")),
            "allotment_date": _clean_str(row.get("ALLOTMENT_")) or None,
            "maturity_date": _clean_str(row.get("MATURE_DT")) or None,
            "lock_in": _clean_str(row.get("LOCK_IN")) == "Y",
            "lock_in_period_days": _clean_num(row.get("LOCK_INPER")),
            "sip_allowed": _clean_str(row.get("SIP_ALLOW")) == "Y",
            "sip_min_amount": _clean_num(row.get("SIP_MINAT")),
            "sip_min_installments": _clean_num(row.get("SIP_MININS")),
            "swp_allowed": _clean_str(row.get("SWP_ALLOW")) == "Y",
            "red_min_amount": _clean_num(row.get("RED_MINAT")),
            "red_settle_days": _clean_num(row.get("SETTLE_PER")),
            "stt_applicable": _clean_str(row.get("STT_APPL")) == "Y",
            "source": "wbr39a",
        }

        # Derive DB.plan_type / DB.option from the scheme NAME itself — more
        # reliable than OPTION_DES (which is often a default placeholder).
        db_plan_type = _derive_plan_type_from_name(scheme_name)
        db_option = _derive_option_from_name(scheme_name, plan_type)

        # Re-lookup scheme using the full unique key (scheme_code, plan_type, option)
        # to distinguish variants like same-code Growth vs IDCW vs Direct vs Regular.
        with session.no_autoflush:
            scheme = session.execute(
                select(Scheme).where(
                    Scheme.scheme_code == scheme_code,
                    Scheme.amc_id == amc.id,
                    Scheme.plan_type.is_(db_plan_type) if db_plan_type is None else Scheme.plan_type == db_plan_type,
                    Scheme.option.is_(db_option) if db_option is None else Scheme.option == db_option,
                )
            ).scalars().first()

        if scheme is None:
            sp = session.begin_nested()
            try:
                scheme = Scheme(
                    scheme_code=scheme_code,
                    isin=isin or None,
                    amc_id=amc.id,
                    name=scheme_name or scheme_code,
                    plan_type=db_plan_type,
                    option=db_option,
                    meta=meta_update,
                )
                session.add(scheme)
                session.flush()
                sp.commit()
                stats["schemes_created"] += 1
            except Exception:
                sp.rollback()
                stats["skipped"] += 1
                continue
        else:
            changed = False
            if scheme_name and scheme.name == scheme.scheme_code:
                scheme.name = scheme_name
                changed = True
            if isin and not scheme.isin:
                scheme.isin = isin
                changed = True
            if db_plan_type and not scheme.plan_type:
                scheme.plan_type = db_plan_type
                changed = True
            if db_option and not scheme.option:
                scheme.option = db_option
                changed = True
            new_meta = dict(scheme.meta or {})
            new_meta.update({k: v for k, v in meta_update.items() if v is not None})
            if new_meta != (scheme.meta or {}):
                scheme.meta = new_meta
                changed = True
            if changed:
                stats["schemes_updated"] += 1

    session.commit()
    return stats


# Static map of KFintech 3-char "Fund" codes to AMC names.
KFINTECH_FUND_NAMES: dict[str, str] = {
    "105": "JM Financial Mutual Fund",
    "107": "Baroda BNP Paribas Mutual Fund",
    "117": "Mirae Asset Mutual Fund",
    "118": "Edelweiss Mutual Fund",
    "119": "Principal Mutual Fund",
    "127": "Motilal Oswal Mutual Fund",
    "132": "Invesco Mutual Fund",
    "137": "Canara Robeco Mutual Fund",
    "143": "Axis Mutual Fund",
    "156": "Sundaram Mutual Fund",
    "166": "quant Mutual Fund",
    "178": "LIC Mutual Fund",
    "182": "PGIM India Mutual Fund",
    "183": "UTI Mutual Fund",
    "RMF": "Nippon India Mutual Fund",
}


def consolidate_kfintech_placeholder_amcs(session) -> dict[str, int]:
    """Fix placeholder AMCs that encode a KFintech Fund code + scheme code.

    Example: AMC 'RMFAFGP' is placeholder from the initial ingest where we
    mistakenly used product_code as amc_code. Split 'RMFAFGP' → fund='RMF'
    + scheme='AFGP', create/find real Nippon India AMC, re-link schemes.
    """
    from sqlalchemy import update as sa_update

    from openreversefeed.db.models import Folio, Scheme, Transaction

    stats = {"amcs_consolidated": 0, "schemes_relinked": 0, "folios_relinked": 0, "txns_relinked": 0}

    amcs = session.execute(select(Amc)).scalars().all()
    real_amc_cache: dict[str, Amc] = {}

    for amc in amcs:
        if not (amc.name.startswith("AMC ") or amc.name == amc.code):
            continue
        # Try to peel off a known KFintech Fund prefix
        fund_code, fund_name = None, None
        for prefix, name in KFINTECH_FUND_NAMES.items():
            if amc.code.startswith(prefix) and len(amc.code) > len(prefix):
                fund_code, fund_name = prefix, name
                break
        if fund_code is None:
            continue

        # Find/create the real AMC
        real = real_amc_cache.get(fund_code)
        if real is None:
            real = session.execute(
                select(Amc).where(Amc.code == fund_code)
            ).scalars().first()
            if real is None:
                real = Amc(code=fund_code, name=fund_name, meta={})
                session.add(real)
                session.flush()
            elif real.name.startswith("AMC ") or real.name == real.code:
                real.name = fund_name
            real_amc_cache[fund_code] = real

        # Re-link schemes + transactions first (no unique constraint issue)
        res = session.execute(
            sa_update(Scheme)
            .where(Scheme.amc_id == amc.id)
            .values(amc_id=real.id)
        )
        stats["schemes_relinked"] += res.rowcount or 0
        res = session.execute(
            sa_update(Transaction)
            .where(Transaction.amc_id == amc.id)
            .values(amc_id=real.id)
        )
        stats["txns_relinked"] += res.rowcount or 0
        session.flush()

        # For folios: merge duplicates before re-linking.
        # A folio with (account_id, folio_number, amc_id=old) may collide with
        # an existing (account_id, folio_number, amc_id=real). Merge them.
        old_folios = session.execute(
            select(Folio).where(Folio.amc_id == amc.id)
        ).scalars().all()
        for of in old_folios:
            existing = session.execute(
                select(Folio).where(
                    Folio.account_id == of.account_id,
                    Folio.folio_number == of.folio_number,
                    Folio.amc_id == real.id,
                    Folio.id != of.id,
                )
            ).scalars().first()
            if existing:
                # Move txns/positions to the existing folio, then delete the dup
                session.execute(
                    sa_update(Transaction)
                    .where(Transaction.folio_id == of.id)
                    .values(folio_id=existing.id)
                )
                from openreversefeed.db.models import Position
                session.execute(
                    sa_update(Position)
                    .where(Position.folio_id == of.id)
                    .values(folio_id=existing.id)
                )
                session.flush()
                session.delete(of)
            else:
                of.amc_id = real.id
            stats["folios_relinked"] += 1
        session.flush()

        session.delete(amc)
        stats["amcs_consolidated"] += 1

    session.commit()
    return stats


# Fallback mapping for CAMS 1-2 character AMC codes. Derived from observed
# scheme-name prefixes in CAMS feeds; extend as needed.
CAMS_AMC_NAMES: dict[str, str] = {
    "B": "Aditya Birla Sun Life Mutual Fund",
    "D": "DSP Mutual Fund",
    "F": "Franklin Templeton Mutual Fund",
    "G": "Bandhan Mutual Fund",
    "H": "HDFC Mutual Fund",
    "I": "ICICI Prudential Mutual Fund",
    "K": "Kotak Mutual Fund",
    "L": "L&T Mutual Fund",
    "M": "Motilal Oswal Mutual Fund",
    "N": "Nippon India Mutual Fund",
    "P": "ICICI Prudential Mutual Fund",
    "PP": "PPFAS Mutual Fund",
    "Q": "quant Mutual Fund",
    "R": "Nippon India Mutual Fund",
    "S": "SBI Mutual Fund",
    "T": "Tata Mutual Fund",
    "U": "UTI Mutual Fund",
}


def consolidate_bad_amcs(session) -> dict[str, int]:
    """Re-link schemes under placeholder AMCs (like 'AMC 105MSGP') to the real AMC.

    Strategy:
      1. Find all "bad" AMCs: code > 3 chars AND name starts with "AMC ".
      2. For each scheme under a bad AMC, find a sibling scheme with the same
         scheme_code (and optionally ISIN) under a GOOD AMC that came from
         WBR39A master. Re-point transactions, folios, positions to the good
         scheme, then delete the orphan scheme.
      3. After all schemes under a bad AMC have been moved away, delete the
         orphan AMC.
    """
    from sqlalchemy import update as sa_update

    from openreversefeed.db.models import (
        Folio,
        Position,
        Transaction,
    )

    stats = {
        "schemes_relinked": 0,
        "schemes_deleted": 0,
        "amcs_deleted": 0,
        "txns_moved": 0,
        "folios_moved": 0,
    }

    bad_amcs = [
        a for a in session.execute(select(Amc)).scalars().all()
        if len(a.code) > 3 and (a.name.startswith("AMC ") or a.name == a.code)
    ]

    for bad_amc in bad_amcs:
        schemes_under_bad = session.execute(
            select(Scheme).where(Scheme.amc_id == bad_amc.id)
        ).scalars().all()

        for bad_scheme in schemes_under_bad:
            # Find a good replacement scheme: same scheme_code, different AMC,
            # whose AMC has a real (non-placeholder) name. Prefer ISIN match.
            good_scheme = None
            if bad_scheme.isin:
                good_scheme = session.execute(
                    select(Scheme)
                    .join(Amc, Amc.id == Scheme.amc_id)
                    .where(Scheme.isin == bad_scheme.isin)
                    .where(Scheme.id != bad_scheme.id)
                    .where(~Amc.name.startswith("AMC "))
                    .limit(1)
                ).scalars().first()
            if good_scheme is None:
                good_scheme = session.execute(
                    select(Scheme)
                    .join(Amc, Amc.id == Scheme.amc_id)
                    .where(Scheme.scheme_code == bad_scheme.scheme_code)
                    .where(Scheme.id != bad_scheme.id)
                    .where(~Amc.name.startswith("AMC "))
                    .limit(1)
                ).scalars().first()

            if good_scheme is None:
                continue  # nothing better found; leave it alone

            # Re-point everything from bad_scheme → good_scheme
            res = session.execute(
                sa_update(Transaction)
                .where(Transaction.scheme_id == bad_scheme.id)
                .values(scheme_id=good_scheme.id, amc_id=good_scheme.amc_id)
            )
            stats["txns_moved"] += res.rowcount or 0

            res = session.execute(
                sa_update(Folio)
                .where(Folio.amc_id == bad_amc.id)
                .values(amc_id=good_scheme.amc_id)
            )
            stats["folios_moved"] += res.rowcount or 0

            session.execute(
                sa_update(Position)
                .where(Position.scheme_id == bad_scheme.id)
                .values(scheme_id=good_scheme.id)
            )

            # Now safe to delete the orphan scheme
            session.delete(bad_scheme)
            stats["schemes_relinked"] += 1
            stats["schemes_deleted"] += 1

        session.flush()

        # If no schemes remain under bad_amc, delete it
        remaining = session.execute(
            select(Scheme).where(Scheme.amc_id == bad_amc.id)
        ).scalars().first()
        if remaining is None:
            session.delete(bad_amc)
            stats["amcs_deleted"] += 1

    session.commit()
    return stats


def apply_cams_amc_fallback(session) -> int:
    """Replace placeholder CAMS AMC names with the static fallback map.

    Only touches AMCs whose code is 1–2 chars AND whose name still equals
    the code or is of the form 'AMC X'.
    """
    amcs = session.execute(select(Amc)).scalars().all()
    updated = 0
    for amc in amcs:
        if len(amc.code) > 2:
            continue
        if amc.name != amc.code and not amc.name.startswith("AMC "):
            continue
        better = CAMS_AMC_NAMES.get(amc.code)
        if better:
            amc.name = better
            updated += 1
    if updated:
        session.commit()
    return updated
