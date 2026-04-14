from datetime import date

import pandas as pd

from openreversefeed.core.aggregation import (
    aggregate_cams_switches,
    aggregate_kfintech_transfers,
)


def _kf_row(**kw):
    base = {
        "transaction_flag": "TI",
        "transaction_number": "T1",
        "parent_transaction_number": "0",
        "folio_number": "F1",
        "transaction_type": "",
        "transaction_date": date(2025, 1, 1),
        "transaction_purred": "P",
        "units": 100.0,
        "amount": 10000.0,
        "transaction_id": "ORIG",
        "registrar_row_index": 0,
    }
    base.update(kw)
    return base


def test_kfintech_aggregates_split_transfer():
    df = pd.DataFrame(
        [
            _kf_row(units=100.0, amount=10000.0, registrar_row_index=0),
            _kf_row(units=50.0, amount=5000.0, registrar_row_index=1),
        ]
    )
    out = aggregate_kfintech_transfers(df)
    assert len(out) == 1
    assert out.iloc[0]["units"] == 150.0
    assert out.iloc[0]["amount"] == 15000.0
    assert out.iloc[0]["original_trans_number"] == "ORIG"


def test_kfintech_ignores_non_transfer_rows():
    df = pd.DataFrame([_kf_row(transaction_flag="", units=100.0, amount=10000.0)])
    out = aggregate_kfintech_transfers(df)
    assert len(out) == 1
    assert out.iloc[0]["units"] == 100.0


def test_cams_aggregates_split_switches():
    df = pd.DataFrame(
        [
            {
                "transaction_id": "SI-1",
                "transaction_type": "SI",
                "transaction_number": "T1",
                "transaction_date": date(2025, 1, 1),
                "units": 100.0,
                "amount": 10000.0,
                "folio_number": "F1",
                "registrar_row_index": 0,
            },
            {
                "transaction_id": "SI-1",
                "transaction_type": "SI",
                "transaction_number": "T1",
                "transaction_date": date(2025, 1, 1),
                "units": 50.0,
                "amount": 5000.0,
                "folio_number": "F1",
                "registrar_row_index": 1,
            },
        ]
    )
    out = aggregate_cams_switches(df)
    assert len(out) == 1
    assert out.iloc[0]["units"] == 150.0
    assert out.iloc[0]["original_trans_number"] == "SI-1"


def test_aggregation_is_deterministic_under_shuffle():
    rows = [_kf_row(units=u, amount=u * 100, registrar_row_index=i) for i, u in enumerate([100.0, 50.0, 75.0])]
    df1 = pd.DataFrame(rows)
    df2 = pd.DataFrame(list(reversed(rows)))
    out1 = aggregate_kfintech_transfers(df1)
    out2 = aggregate_kfintech_transfers(df2)
    assert out1["units"].iloc[0] == out2["units"].iloc[0] == 225.0
