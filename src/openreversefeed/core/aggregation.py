"""Aggregation of partial transfer/switch records. See spec §5 step 4d."""
from __future__ import annotations

import pandas as pd

_TRANSFER_FLAGS = {"TI", "SI", "SO", "TO"}


def _stable_sort(df: pd.DataFrame, keys: list[str]) -> pd.DataFrame:
    return df.sort_values(keys, kind="mergesort").reset_index(drop=True)


def aggregate_kfintech_transfers(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty or "transaction_flag" not in df.columns:
        return df.copy()

    transfer_mask = df["transaction_flag"].isin(_TRANSFER_FLAGS)
    transfers = df[transfer_mask]
    non_transfers = df[~transfer_mask]

    if transfers.empty:
        out = df.copy()
        if "original_trans_number" not in out.columns:
            out["original_trans_number"] = out["transaction_id"]
        return out

    sorted_t = _stable_sort(transfers, ["transaction_date", "registrar_row_index"])

    group_keys = [
        "transaction_purred",
        "transaction_number",
        "parent_transaction_number",
        "folio_number",
        "transaction_type",
        "transaction_date",
    ]
    sum_cols = ["units", "amount"]
    first_cols = [c for c in sorted_t.columns if c not in group_keys + sum_cols]

    agg_map: dict[str, str] = {c: "sum" for c in sum_cols}
    agg_map.update({c: "first" for c in first_cols})

    # dropna=False is critical — otherwise rows with NaN in any group key
    # (e.g. no parent_transaction_number on a solo Lateral Shift Out) are
    # silently dropped, making transfers/switches disappear from the ledger.
    aggregated = sorted_t.groupby(group_keys, as_index=False, sort=False, dropna=False).agg(agg_map)
    aggregated["original_trans_number"] = aggregated["transaction_id"]

    nt = non_transfers.copy()
    if "original_trans_number" not in nt.columns:
        nt["original_trans_number"] = nt["transaction_id"]

    return pd.concat([aggregated, nt], ignore_index=True)


def aggregate_cams_switches(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty or "transaction_type" not in df.columns:
        return df.copy()

    switch_mask = df["transaction_type"].isin({"SI", "SO"})
    switches = df[switch_mask]
    non_switches = df[~switch_mask]

    if switches.empty:
        out = df.copy()
        if "original_trans_number" not in out.columns:
            out["original_trans_number"] = out["transaction_id"]
        return out

    sorted_s = _stable_sort(switches, ["transaction_date", "registrar_row_index"])

    group_keys = ["transaction_id", "transaction_type", "transaction_number", "transaction_date"]
    sum_cols = ["units", "amount"]
    first_cols = [c for c in sorted_s.columns if c not in group_keys + sum_cols]

    agg_map: dict[str, str] = {c: "sum" for c in sum_cols}
    agg_map.update({c: "first" for c in first_cols})

    # dropna=False — see aggregate_kfintech_transfers above for rationale.
    aggregated = sorted_s.groupby(group_keys, as_index=False, sort=False, dropna=False).agg(agg_map)
    aggregated["original_trans_number"] = aggregated["transaction_id"]

    ns = non_switches.copy()
    if "original_trans_number" not in ns.columns:
        ns["original_trans_number"] = ns["transaction_id"]

    return pd.concat([aggregated, ns], ignore_index=True)
