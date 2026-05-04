"""KFintech WSTPR (Web Systematic-plan Register) ingester.

The WSTPR file lists every SIP / STP / SWP registration under an ARN. Unlike
transaction feeds (which describe past activity), this file describes
*forward-looking commitments*: what plans are active, when they end, how much
per installment, and who the bank-mandate is set up under.

We use it to:
  * Populate account profile data — email, mobile, postal address, bank info
  * Power the SIP Book section on each holding-detail page
  * Add commitment metrics to the Book Overview (active SIP count, monthly
    inflow committed, plans ending soon, paused/cancelled plans needing
    follow-up).

Storage strategy: rather than introducing a new SQL table (which would need
a migration), we stash SIP registrations on ``Account.meta['systematic_plans']``
as a list of dicts. Address goes on ``Account.meta['address']``. This keeps
schema changes off the critical path and makes the data accessible from
existing JSONB-aware views.
"""
from __future__ import annotations

import datetime as dt
import math
import re
from typing import Any

import pandas as pd
from sqlalchemy import select

from openreversefeed.db.models import Account, Folio, Scheme

# Distinctive headers — used by the ingest pipeline to route this file type.
_WSTPR_REQUIRED = {
    "Prodcode", "Acno", "SchCode", "TrType", "STARTDATE",
    "AMOUNT", "PAIDINST", "InvName",
}


def is_wstpr_file(headers: set[str]) -> bool:
    """Return True if the column set matches a KFintech WSTPR registration file."""
    cleaned = {h.strip() for h in headers}
    return _WSTPR_REQUIRED.issubset(cleaned)


def _clean(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and math.isnan(v):
        return ""
    s = str(v).strip()
    if s.lower() == "nan":
        return ""
    return s


def _parse_date(s: str) -> dt.date | None:
    if not s:
        return None
    s = s.strip()
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%y"):
        try:
            return dt.datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _normalize_name(name: str) -> str:
    """Lowercase + collapse whitespace for fuzzy account matching."""
    return re.sub(r"\s+", " ", (name or "").strip().lower())


def _classify_status(end_date: dt.date | None, remarks: str, paid: int, pending: int) -> str:
    """Active / Paused / Cancelled / Completed."""
    today = dt.date.today()
    rmk_lower = (remarks or "").lower()
    if "stop" in rmk_lower or "cancel" in rmk_lower or "ceased" in rmk_lower:
        return "cancelled"
    if "paus" in rmk_lower or "hold" in rmk_lower:
        return "paused"
    if end_date and end_date < today:
        return "completed"
    if pending == 0 and paid > 0:
        return "completed"
    return "active"


def ingest_wstpr(session, raw_df: pd.DataFrame) -> dict[str, Any]:
    """Sync WSTPR rows into Accounts (profile + systematic_plans) and Folios.

    Idempotent: re-running with the same file does not duplicate plans
    (we key by IHNO — KFintech's internal registration reference).
    """
    stats = {
        "rows_in": int(len(raw_df)),
        "accounts_matched": 0,
        "accounts_unmatched": 0,
        "plans_added": 0,
        "plans_updated": 0,
        "addresses_filled": 0,
        "emails_filled": 0,
        "phones_filled": 0,
        "bank_mandates_added": 0,
    }

    # Build fast name → account index
    accounts = session.execute(select(Account)).scalars().all()
    by_name: dict[str, list[Account]] = {}
    for a in accounts:
        key = _normalize_name(a.name)
        if key:
            by_name.setdefault(key, []).append(a)

    for _, row in raw_df.iterrows():
        inv_name = _clean(row.get("InvName"))
        if not inv_name:
            stats["accounts_unmatched"] += 1
            continue

        candidates = by_name.get(_normalize_name(inv_name), [])
        if not candidates:
            stats["accounts_unmatched"] += 1
            continue
        # If multiple accounts share the same name, prefer the one whose folio
        # number matches the Acno on this row.
        folio_no = _clean(row.get("Acno"))
        account = candidates[0]
        if len(candidates) > 1 and folio_no:
            for c in candidates:
                fmatch = session.execute(
                    select(Folio).where(
                        Folio.account_id == c.id,
                        Folio.folio_number == folio_no,
                    )
                ).scalars().first()
                if fmatch:
                    account = c
                    break
        stats["accounts_matched"] += 1

        # ─── Profile fields (only fill missing) ───
        email = _clean(row.get("Email"))
        if email and not account.email:
            account.email = email
            stats["emails_filled"] += 1

        phone = _clean(row.get("OffPhone")) or _clean(row.get("ResPhone"))
        if phone and not account.phone:
            account.phone = phone
            stats["phones_filled"] += 1

        meta = dict(account.meta or {})

        addr_parts = [
            _clean(row.get("Add1")),
            _clean(row.get("Add2")),
            _clean(row.get("Add3")),
        ]
        addr_parts = [p for p in addr_parts if p]
        addr = {
            "street": ", ".join(addr_parts),
            "city": _clean(row.get("City")),
            "state": _clean(row.get("State")),
            "pincode": _clean(row.get("Pin")),
        }
        if any(addr.values()) and "address" not in meta:
            meta["address"] = addr
            stats["addresses_filled"] += 1

        # Bank mandate (only add if we have a real bank name)
        bank_name = _clean(row.get("ECSBANKNAMe"))
        if bank_name:
            mandates = list(meta.get("bank_mandates", []))
            ac_no = _clean(row.get("ECSACNO"))
            holder = _clean(row.get("ECSHolderName"))
            mandate_key = (bank_name, ac_no)
            already = any(
                (m.get("bank") == bank_name and m.get("account_no") == ac_no)
                for m in mandates
            )
            if not already:
                mandates.append({
                    "bank": bank_name,
                    "account_no": ac_no,
                    "holder_name": holder,
                    "added_via": "WSTPR",
                })
                meta["bank_mandates"] = mandates
                stats["bank_mandates_added"] += 1

        # ─── Systematic plan record ───
        ihno = _clean(row.get("IHNO"))
        start_d = _parse_date(_clean(row.get("STARTDATE")))
        end_d = _parse_date(_clean(row.get("ENDDATE")))
        try:
            paid = int(float(_clean(row.get("PAIDINST")) or "0"))
        except ValueError:
            paid = 0
        try:
            pending = int(float(_clean(row.get("PENDINST")) or "0"))
        except ValueError:
            pending = 0
        try:
            instalno = int(float(_clean(row.get("INSTALNO")) or "0"))
        except ValueError:
            instalno = 0
        try:
            amount = float(_clean(row.get("AMOUNT")) or "0")
        except ValueError:
            amount = 0.0

        remarks = _clean(row.get("REMARKS"))
        status = _classify_status(end_d, remarks, paid, pending)

        # Resolve our DB scheme_code: WSTPR's SchCode is KFintech's internal
        # code (e.g. "1098") which doesn't match our DB. Prodcode = Fund + our
        # scheme_code (e.g. "166PEGP" → strip "166" prefix to get "PEGP").
        prodcode = _clean(row.get("Prodcode"))
        fund = _clean(row.get("Fund"))
        sch_code_raw = _clean(row.get("SchCode"))
        if prodcode and fund and prodcode.startswith(fund):
            db_scheme_code = prodcode[len(fund):]
        else:
            db_scheme_code = sch_code_raw

        plan = {
            "tr_type": _clean(row.get("TrType")).upper() or "SIP",
            "freq": _clean(row.get("Freq")),
            "scheme_code": db_scheme_code,
            "raw_sch_code": sch_code_raw,
            "scheme_name": _clean(row.get("SchDesc")),
            "fund_code": fund,
            "folio_number": folio_no,
            "amount": amount,
            "start_date": start_d.isoformat() if start_d else None,
            "end_date": end_d.isoformat() if end_d else None,
            "paid_installments": paid,
            "pending_installments": pending,
            "total_installments": instalno,
            "sip_type": _clean(row.get("SIPType")),  # PERPETUAL / NORMAL
            "remarks": remarks,
            "registration_no": ihno,
            "registration_date": _parse_date(_clean(row.get("RegDate"))).isoformat()
                if _parse_date(_clean(row.get("RegDate"))) else None,
            "stp_target_scheme": _clean(row.get("STPInScheme")),
            "city_category": _clean(row.get("CityCategory")),
            "status": status,
        }

        plans = list(meta.get("systematic_plans", []))
        # Idempotent: dedupe on registration_no (IHNO) if present, else
        # (scheme_code, start_date, amount). Also clear out any older entries
        # that were stored with the wrong scheme code (raw KFintech SchCode).
        existing_idx = None
        for i, p in enumerate(plans):
            if ihno and p.get("registration_no") == ihno:
                existing_idx = i
                break
            # Also match by old (raw_sch_code) for migration of stale rows
            if (p.get("scheme_code") in (plan["scheme_code"], sch_code_raw)
                and p.get("start_date") == plan["start_date"]
                and p.get("amount") == plan["amount"]):
                existing_idx = i
                break
        if existing_idx is not None:
            plans[existing_idx] = plan
            stats["plans_updated"] += 1
        else:
            plans.append(plan)
            stats["plans_added"] += 1

        meta["systematic_plans"] = plans
        account.meta = meta

    session.commit()
    return stats
