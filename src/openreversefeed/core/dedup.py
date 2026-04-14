"""In-file deduplication — drops duplicate composite_keys."""
from __future__ import annotations

import pandas as pd


def drop_in_file_duplicates(df: pd.DataFrame) -> pd.DataFrame:
    """Return a new DataFrame with rows having duplicate composite_key dropped.

    Keeps the first occurrence (stable). Preserves column order.
    """
    if df.empty or "composite_key" not in df.columns:
        return df.copy()
    return df.drop_duplicates(subset=["composite_key"], keep="first").reset_index(drop=True)
