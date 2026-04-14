import pandas as pd

from openreversefeed.core.pair_removal import remove_cams_pairs


def _row(txn_no, mode, units, amount, folio="F1", txn_type="R"):
    return {
        "transaction_number": txn_no,
        "transaction_mode": mode,
        "transaction_type": txn_type,
        "units": units,
        "amount": amount,
        "folio_number": folio,
    }


def test_removes_cams_pair_same_folio_and_number():
    df = pd.DataFrame(
        [
            _row("T1", "M", 10.0, 1000.0),
            _row("T1", "R", -10.0, -1000.0),
        ]
    )
    out = remove_cams_pairs(df)
    assert out.empty


def test_cams_unmatched_rows_stay():
    df = pd.DataFrame(
        [
            _row("T1", "N", 10.0, 1000.0),
            _row("T2", "R", -10.0, -1000.0),
        ]
    )
    out = remove_cams_pairs(df)
    assert len(out) == 2


def test_cams_empty_input():
    df = pd.DataFrame(
        columns=[
            "transaction_number",
            "transaction_mode",
            "transaction_type",
            "units",
            "amount",
            "folio_number",
        ]
    )
    out = remove_cams_pairs(df)
    assert out.empty
