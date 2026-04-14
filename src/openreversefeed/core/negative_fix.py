"""Negative value correction. See spec §5 step 4c."""
from __future__ import annotations

import pandas as pd


def correct_negative_rows(df: pd.DataFrame, type_flip_map: dict[str, str]) -> pd.DataFrame:
    """Flip sign + flip type + mark as reversal when both units and amount are negative."""
    if df.empty:
        return df.copy()
    out = df.copy()
    mask = (
        (out["transaction_mode"] == "N") & (out["units"] < 0) & (out["amount"] < 0)
    )
    if not mask.any():
        return out

    out.loc[mask, "units"] = -out.loc[mask, "units"]
    out.loc[mask, "amount"] = -out.loc[mask, "amount"]
    out.loc[mask, "transaction_type"] = out.loc[mask, "transaction_type"].map(
        lambda t: type_flip_map.get(t, t)
    )
    out.loc[mask, "transaction_mode"] = "R"
    return out
