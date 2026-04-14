"""Vectorized redemption+reversal pair removal for KFintech and CAMS."""
from __future__ import annotations

import numpy as np
import pandas as pd

_TOLERANCE = 1e-6


def remove_kfintech_pairs(df: pd.DataFrame) -> pd.DataFrame:
    """Drop matched (redemption, reversal) pairs for KFintech.

    A pair forms when:
    - A row has transaction_mode='R' and a non-empty parent_transaction_number
    - It matches a prior row where transaction_number == parent_transaction_number
      AND mode in ('M','R') AND units/amount are opposite within TOLERANCE
      AND folio_number matches
    """
    if df.empty:
        return df.copy()

    sorted_df = df.reset_index(drop=True).copy()
    sorted_df["_row_id"] = sorted_df.index

    reversals = sorted_df[
        (sorted_df["transaction_mode"] == "R")
        & sorted_df["parent_transaction_number"].notna()
        & (sorted_df["parent_transaction_number"] != "")
    ]

    if reversals.empty:
        return sorted_df.drop(columns=["_row_id"])

    candidates = sorted_df[sorted_df["transaction_mode"].isin(["M", "R"])]

    merged = reversals.merge(
        candidates,
        left_on=["parent_transaction_number", "folio_number"],
        right_on=["transaction_number", "folio_number"],
        suffixes=("_rev", "_orig"),
    )

    matched = merged[
        (np.isclose(merged["units_rev"], -merged["units_orig"], atol=_TOLERANCE))
        & (np.isclose(merged["amount_rev"], -merged["amount_orig"], atol=_TOLERANCE))
        & (merged["_row_id_rev"] != merged["_row_id_orig"])
    ]

    if matched.empty:
        return sorted_df.drop(columns=["_row_id"])

    to_drop = pd.unique(
        np.concatenate([matched["_row_id_rev"].values, matched["_row_id_orig"].values])
    )
    result = sorted_df[~sorted_df["_row_id"].isin(to_drop)].drop(columns=["_row_id"])
    return result.reset_index(drop=True)


def remove_cams_pairs(df: pd.DataFrame) -> pd.DataFrame:
    """Drop matched (redemption, reversal) pairs for CAMS.

    CAMS has no parent_transaction_number, so match within each
    (folio_number, transaction_type) group: a row with mode in ('M','R') and
    positive units pairs with a mode='R' row of opposite sign with the same
    transaction_number, within tolerance.
    """
    if df.empty:
        return df.copy()

    sorted_df = df.reset_index(drop=True).copy()
    sorted_df["_row_id"] = sorted_df.index

    originals = sorted_df[
        sorted_df["transaction_mode"].isin(["M", "R"]) & (sorted_df["units"] > 0)
    ]
    reversals = sorted_df[
        (sorted_df["transaction_mode"] == "R") & (sorted_df["units"] < 0)
    ]

    if originals.empty or reversals.empty:
        return sorted_df.drop(columns=["_row_id"])

    merged = originals.merge(
        reversals,
        on=["folio_number", "transaction_type", "transaction_number"],
        suffixes=("_orig", "_rev"),
    )

    matched = merged[
        (np.isclose(merged["units_orig"], -merged["units_rev"], atol=_TOLERANCE))
        & (np.isclose(merged["amount_orig"], -merged["amount_rev"], atol=_TOLERANCE))
        & (merged["_row_id_orig"] != merged["_row_id_rev"])
    ]

    if matched.empty:
        return sorted_df.drop(columns=["_row_id"])

    to_drop = pd.unique(
        np.concatenate([matched["_row_id_orig"].values, matched["_row_id_rev"].values])
    )
    return (
        sorted_df[~sorted_df["_row_id"].isin(to_drop)]
        .drop(columns=["_row_id"])
        .reset_index(drop=True)
    )
