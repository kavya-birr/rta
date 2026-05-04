"""Backfill scheme names and ISINs from source files into the schemes table.

This fixes the fact that auto-created schemes only have ``scheme_code`` stored
as both code and name. By scanning the already-ingested source files, we can
populate the proper scheme name (used for AMFI matching) and ISIN when
available.

Called on-demand from the clients views — idempotent and fast (~50ms per run).
"""
from __future__ import annotations

import glob
from pathlib import Path

import pandas as pd
from dbfread import DBF
from django.conf import settings
from sqlalchemy import select, update

from openreversefeed.db.models import Account, Folio, Scheme, Transaction


def _strip_quotes(df: pd.DataFrame) -> pd.DataFrame:
    stripped_cols = {c: c.strip("'\" ") for c in df.columns}
    if any(k != v for k, v in stripped_cols.items()):
        df = df.rename(columns=stripped_cols)
        for col in df.columns:
            if df[col].dtype == object:
                df[col] = df[col].str.strip("'\" ")
    return df


def build_scheme_map_from_sources() -> dict[str, dict]:
    """Scan all uploaded source files and build {scheme_code: {name, isin}}."""
    upload_dir = Path(settings.UPLOAD_DIR)
    result: dict[str, dict] = {}

    for f in glob.glob(str(upload_dir / "*.csv")):
        try:
            df = _strip_quotes(pd.read_csv(f, dtype=str))
        except Exception:
            continue

        # CAMS CSV: PRODCODE + SCHEME
        if "PRODCODE" in df.columns and "SCHEME" in df.columns:
            for _, row in df[["PRODCODE", "SCHEME"]].drop_duplicates().iterrows():
                code = str(row["PRODCODE"]).strip()
                name = str(row["SCHEME"]).strip()
                if code and name and name.lower() != "nan":
                    result.setdefault(code, {})["name"] = name

        # KFintech WBTRN CSV: Scheme Code + Fund Description + ISIN
        if "Scheme Code" in df.columns and "Fund Description" in df.columns:
            cols = ["Scheme Code", "Fund Description"]
            if "ISIN" in df.columns:
                cols.append("ISIN")
            for _, row in df[cols].drop_duplicates().iterrows():
                code = str(row["Scheme Code"]).strip()
                name = str(row["Fund Description"]).strip()
                isin = str(row.get("ISIN", "")).strip() if "ISIN" in df.columns else ""
                if code and name and name.lower() != "nan":
                    result.setdefault(code, {})["name"] = name
                if code and isin and isin.lower() != "nan":
                    result.setdefault(code, {})["isin"] = isin

        # KFintech standard CSV: Scheme Code + alternate name column
        if "Scheme Code" in df.columns and "Scheme" in df.columns:
            for _, row in df[["Scheme Code", "Scheme"]].drop_duplicates().iterrows():
                code = str(row["Scheme Code"]).strip()
                name = str(row["Scheme"]).strip()
                if code and name and name.lower() != "nan":
                    result.setdefault(code, {}).setdefault("name", name)

    # CAMS DBF: PRODCODE + SCHEME
    for f in glob.glob(str(upload_dir / "*.dbf")):
        try:
            for row in DBF(f, load=True, char_decode_errors="ignore"):
                code = str(row.get("PRODCODE", "")).strip()
                name = str(row.get("SCHEME", "")).strip()
                if code and name:
                    result.setdefault(code, {})["name"] = name
        except Exception:
            continue

    return result


def backfill_scheme_names(session) -> int:
    """Update Scheme.name and Scheme.isin from source files. Returns count updated."""
    mapping = build_scheme_map_from_sources()
    if not mapping:
        return 0

    schemes = session.execute(select(Scheme)).scalars().all()
    updated = 0

    for scheme in schemes:
        info = mapping.get(scheme.scheme_code)
        if not info:
            continue
        changed = False
        new_name = info.get("name")
        new_isin = info.get("isin")
        # Only update if the current name is still equal to the code (default
        # placeholder from auto-creation) — never overwrite a real name.
        if new_name and scheme.name == scheme.scheme_code and new_name != scheme.scheme_code:
            scheme.name = new_name
            changed = True
        if new_isin and not scheme.isin:
            scheme.isin = new_isin
            changed = True
        if changed:
            updated += 1

    if updated:
        session.commit()
    return updated


def backfill_isins_from_amfi(session) -> int:
    """For every scheme that has no ISIN but has a usable name, look the name
    up in the AMFI NAV feed (via the existing fuzzy matcher) and store the
    matched ISIN. Runs idempotently — only touches rows where ``isin`` is
    NULL or empty.
    """
    from clients.amfi_nav import lookup_nav

    schemes = session.execute(
        select(Scheme).where(
            (Scheme.isin.is_(None)) | (Scheme.isin == ""),
        )
    ).scalars().all()

    updated = 0
    for scheme in schemes:
        name = scheme.name or scheme.scheme_code
        if not name or name == scheme.scheme_code:
            continue  # nothing to fuzzy-match on
        nav, _date, matched_name = lookup_nav(scheme.scheme_code, name, None)
        if not matched_name:
            continue
        # Look up the AMFI record again to pull the ISIN of the matched scheme
        from clients.amfi_nav import load_nav_map

        nav_map = load_nav_map()
        record = nav_map["by_name"].get(matched_name.lower().strip())
        if record is None:
            # Try the normalized lookup to get same record
            continue
        # Prefer the Growth-option ISIN, else the Reinvest one
        new_isin = record.get("isin_growth") or record.get("isin_reinvest")
        if new_isin and new_isin != "-":
            scheme.isin = new_isin
            updated += 1

    if updated:
        session.commit()
    return updated


def deduplicate_accounts(session) -> int:
    """Merge accounts that share the same PAN + ownership_type.

    The auto-creation path in ``ofr_bridge.py`` can create a new Account row
    on each ingest run if the prior row existed in an uncommitted session. The
    accounts table has no DB-level unique constraint on PAN (family PAN support
    requires multiple rows under the same PAN), so we dedupe only when name AND
    ownership_type also match — treating those as "the same investor".

    For each duplicate cluster we pick the oldest account as the canonical
    one, re-point all transactions and folios to it, then delete the orphans.
    """
    from collections import defaultdict

    from sqlalchemy import update as sa_update

    accounts = session.execute(
        select(Account).order_by(Account.created_at)
    ).scalars().all()

    groups: dict[tuple, list[Account]] = defaultdict(list)
    for a in accounts:
        if not a.pan:
            continue
        key = (a.pan.upper(), (a.name or "").strip().lower(), a.ownership_type or "")
        groups[key].append(a)

    merged = 0
    for group in groups.values():
        if len(group) < 2:
            continue
        primary = group[0]  # oldest
        for dup in group[1:]:
            # Re-point transactions + folios to primary
            session.execute(
                sa_update(Transaction)
                .where(Transaction.account_id == dup.id)
                .values(account_id=primary.id)
            )
            # Folios have UNIQUE(account_id, folio_number, amc_id) — merge dup
            # folios into primary if primary already has a matching one
            dup_folios = session.execute(
                select(Folio).where(Folio.account_id == dup.id)
            ).scalars().all()
            for df in dup_folios:
                existing = session.execute(
                    select(Folio).where(
                        Folio.account_id == primary.id,
                        Folio.folio_number == df.folio_number,
                        Folio.amc_id == df.amc_id,
                    )
                ).scalars().first()
                if existing:
                    session.execute(
                        sa_update(Transaction)
                        .where(Transaction.folio_id == df.id)
                        .values(folio_id=existing.id)
                    )
                    session.flush()
                    session.delete(df)
                else:
                    df.account_id = primary.id
            session.flush()
            session.delete(dup)
            merged += 1

    if merged:
        session.commit()
    return merged
