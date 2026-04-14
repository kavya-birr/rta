import pandas as pd

from openreversefeed.core.conflict import resolve_kfintech_conflicts


def test_keeps_different_folio_rows_with_same_txn_number():
    df = pd.DataFrame(
        [
            {
                "transaction_number": "T1",
                "folio_number": "F1",
                "transaction_purred": "P",
                "units": 10.0,
            },
            {
                "transaction_number": "T1",
                "folio_number": "F2",
                "transaction_purred": "P",
                "units": 20.0,
            },
        ]
    )
    out = resolve_kfintech_conflicts(df)
    assert len(out) == 2


def test_dedups_p_sin_within_same_folio_same_txn():
    df = pd.DataFrame(
        [
            {
                "transaction_number": "T1",
                "folio_number": "F1",
                "transaction_purred": "P",
                "units": 10.0,
            },
            {
                "transaction_number": "T1",
                "folio_number": "F1",
                "transaction_purred": "SIN",
                "units": 10.0,
            },
        ]
    )
    out = resolve_kfintech_conflicts(df)
    assert len(out) == 1
    assert out.iloc[0]["transaction_purred"] == "P"


def test_empty_input():
    df = pd.DataFrame(
        columns=["transaction_number", "folio_number", "transaction_purred", "units"]
    )
    out = resolve_kfintech_conflicts(df)
    assert out.empty
