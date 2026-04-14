from datetime import date

import pandas as pd

from openreversefeed.adapters.cams import CamsAdapter
from openreversefeed.core.cleaner import Cleaner


def test_cleaner_runs_all_steps_on_minimal_cams_df():
    cleaner = Cleaner()
    adapter = CamsAdapter()
    df = pd.DataFrame(
        [
            {
                "transaction_id": "1",
                "folio_number": "F1",
                "product_code": "P1",
                "scheme_code": "S1",
                "units": 100.0,
                "amount": 10000.0,
                "transaction_date": date(2025, 1, 1),
                "transaction_mode": "N",
                "transaction_type": "P",
                "transaction_number": "T1",
                "registrar_row_index": 0,
                "__source_meta": [{}],
            }
        ]
    )
    out = cleaner.run(df, adapter)
    assert not out.empty
    assert "composite_key" in out.columns
    assert "action" in out.columns
    assert "action_tag" in out.columns
