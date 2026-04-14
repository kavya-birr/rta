"""KFintech conflict resolution — P+SIN dedup. See spec §5 step 4e."""
from __future__ import annotations

import pandas as pd


def resolve_kfintech_conflicts(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df.copy()

    required = {"transaction_number", "folio_number", "transaction_purred"}
    if not required.issubset(df.columns):
        return df.copy()

    def _pick(group: pd.DataFrame) -> pd.DataFrame:
        purreds = set(group["transaction_purred"])
        has_sin = any(
            isinstance(p, str) and (p == "SIN" or p.endswith("SIN")) for p in purreds
        )
        if "P" in purreds and has_sin:
            return group[group["transaction_purred"] == "P"].head(1)
        return group

    return (
        df.groupby(["transaction_number", "folio_number"], sort=False, group_keys=False)
        .apply(_pick, include_groups=False)
        .reset_index(drop=True)
    )
