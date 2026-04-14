from datetime import date

import pandas as pd

from openreversefeed.core.composite_key import (
    assign_composite_keys,
    build_cams_key,
    build_kfintech_key,
)
from openreversefeed.core.models import Registrar


def test_cams_key_deterministic():
    row = {
        "original_trans_number": "1997865738",
        "transaction_type": "SI",
        "transaction_number": "1311531177",
        "transaction_date": date(2025, 7, 29),
    }
    assert build_cams_key(row) == "1997865738_SI_1311531177_20250729"


def test_kfintech_key_deterministic():
    row = {
        "transaction_number": "1227",
        "parent_transaction_number": "0",
        "folio_number": "91046479506",
        "transaction_date": date(2020, 7, 8),
    }
    assert build_kfintech_key(row) == "1227_0_91046479506_20200708"


def test_kfintech_key_none_parent_becomes_zero():
    row = {
        "transaction_number": "1227",
        "parent_transaction_number": None,
        "folio_number": "91046479506",
        "transaction_date": date(2020, 7, 8),
    }
    assert build_kfintech_key(row) == "1227_0_91046479506_20200708"


def test_assign_keys_writes_column_for_cams():
    df = pd.DataFrame(
        {
            "original_trans_number": ["1997865738"],
            "transaction_type": ["SI"],
            "transaction_number": ["1311531177"],
            "transaction_date": [date(2025, 7, 29)],
        }
    )
    out = assign_composite_keys(df, Registrar.CAMS)
    assert "composite_key" in out.columns
    assert out["composite_key"].iloc[0] == "1997865738_SI_1311531177_20250729"
    assert "composite_key" not in df.columns  # pure function


def test_assign_keys_writes_column_for_kfintech():
    df = pd.DataFrame(
        {
            "transaction_number": ["1227"],
            "parent_transaction_number": ["0"],
            "folio_number": ["91046479506"],
            "transaction_date": [date(2020, 7, 8)],
        }
    )
    out = assign_composite_keys(df, Registrar.KFINTECH)
    assert out["composite_key"].iloc[0] == "1227_0_91046479506_20200708"
