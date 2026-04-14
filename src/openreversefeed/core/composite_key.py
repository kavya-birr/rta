"""Deterministic composite key builders. See spec §5 step 5."""
from __future__ import annotations

from typing import Any

import pandas as pd

from openreversefeed.core.models import Registrar


def _date_str(value: Any) -> str:
    if hasattr(value, "strftime"):
        return value.strftime("%Y%m%d")
    return str(value)


def build_cams_key(row: dict[str, Any]) -> str:
    return (
        f"{row['original_trans_number']}_{row['transaction_type']}_"
        f"{row['transaction_number']}_{_date_str(row['transaction_date'])}"
    )


def build_kfintech_key(row: dict[str, Any]) -> str:
    parent = row.get("parent_transaction_number") or "0"
    return (
        f"{row['transaction_number']}_{parent}_{row['folio_number']}_"
        f"{_date_str(row['transaction_date'])}"
    )


def assign_composite_keys(df: pd.DataFrame, registrar: Registrar) -> pd.DataFrame:
    out = df.copy()
    if registrar is Registrar.CAMS:
        out["composite_key"] = out.apply(lambda r: build_cams_key(r.to_dict()), axis=1)
    elif registrar is Registrar.KFINTECH:
        out["composite_key"] = out.apply(lambda r: build_kfintech_key(r.to_dict()), axis=1)
    else:
        raise ValueError(f"unknown registrar: {registrar}")
    return out
