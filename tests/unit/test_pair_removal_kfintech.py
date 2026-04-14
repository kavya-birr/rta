from datetime import date

import pandas as pd

from openreversefeed.core.pair_removal import remove_kfintech_pairs


def _row(txn_no, parent, mode, units, amount, folio="F1"):
    return {
        "transaction_number": txn_no,
        "parent_transaction_number": parent,
        "transaction_mode": mode,
        "units": units,
        "amount": amount,
        "folio_number": folio,
        "transaction_date": date(2025, 1, 1),
    }


def test_removes_matched_redemption_reversal_pair():
    df = pd.DataFrame(
        [
            _row("100", None, "M", -10.0, -1000.0),  # original redemption (mode M)
            _row("200", "100", "R", 10.0, 1000.0),  # reversal
        ]
    )
    out = remove_kfintech_pairs(df)
    assert out.empty


def test_leaves_unmatched_rows_alone():
    df = pd.DataFrame(
        [
            _row("100", None, "M", -10.0, -1000.0),
            _row("300", "999", "R", 10.0, 1000.0),  # parent doesn't match
        ]
    )
    out = remove_kfintech_pairs(df)
    assert len(out) == 2


def test_out_of_tolerance_not_matched():
    df = pd.DataFrame(
        [
            _row("100", None, "M", -10.0, -1000.0),
            _row("200", "100", "R", 10.5, 1000.0),  # units differ
        ]
    )
    out = remove_kfintech_pairs(df)
    assert len(out) == 2


def test_empty_input():
    df = pd.DataFrame(
        columns=[
            "transaction_number",
            "parent_transaction_number",
            "transaction_mode",
            "units",
            "amount",
            "folio_number",
            "transaction_date",
        ]
    )
    out = remove_kfintech_pairs(df)
    assert out.empty
