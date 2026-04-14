import pandas as pd

from openreversefeed.core.negative_fix import correct_negative_rows

FLIP = {"P": "R", "R": "P", "SI": "SO", "SO": "SI", "TI": "TO", "TO": "TI"}


def test_flips_both_negative_row():
    df = pd.DataFrame(
        [
            {
                "transaction_mode": "N",
                "units": -10.0,
                "amount": -1000.0,
                "transaction_type": "P",
            }
        ]
    )
    out = correct_negative_rows(df, FLIP)
    assert out.iloc[0]["units"] == 10.0
    assert out.iloc[0]["amount"] == 1000.0
    assert out.iloc[0]["transaction_type"] == "R"
    assert out.iloc[0]["transaction_mode"] == "R"


def test_leaves_half_negative_alone():
    df = pd.DataFrame(
        [
            {
                "transaction_mode": "N",
                "units": -10.0,
                "amount": 1000.0,
                "transaction_type": "P",
            },
            {
                "transaction_mode": "N",
                "units": 10.0,
                "amount": -1000.0,
                "transaction_type": "P",
            },
        ]
    )
    out = correct_negative_rows(df, FLIP)
    assert out.iloc[0]["units"] == -10.0
    assert out.iloc[0]["transaction_mode"] == "N"
    assert out.iloc[1]["amount"] == -1000.0


def test_only_normal_mode_processed():
    df = pd.DataFrame(
        [
            {
                "transaction_mode": "R",
                "units": -10.0,
                "amount": -1000.0,
                "transaction_type": "P",
            }
        ]
    )
    out = correct_negative_rows(df, FLIP)
    assert out.iloc[0]["units"] == -10.0


def test_unknown_type_leaves_type_alone():
    df = pd.DataFrame(
        [
            {
                "transaction_mode": "N",
                "units": -10.0,
                "amount": -1000.0,
                "transaction_type": "UNK",
            }
        ]
    )
    out = correct_negative_rows(df, FLIP)
    assert out.iloc[0]["units"] == 10.0
    assert out.iloc[0]["transaction_type"] == "UNK"
