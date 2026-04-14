import pandas as pd

from openreversefeed.core.dedup import drop_in_file_duplicates


def test_drop_duplicates_removes_exact_copies():
    df = pd.DataFrame(
        {
            "composite_key": ["A", "A", "B", "C"],
            "units": [100.0, 100.0, 50.0, 25.0],
        }
    )
    out = drop_in_file_duplicates(df)
    assert list(out["composite_key"]) == ["A", "B", "C"]
    assert len(out) == 3


def test_drop_duplicates_empty_input():
    df = pd.DataFrame(columns=["composite_key", "units"])
    out = drop_in_file_duplicates(df)
    assert out.empty
    assert list(out.columns) == ["composite_key", "units"]


def test_drop_duplicates_preserves_first_occurrence_order():
    df = pd.DataFrame(
        {
            "composite_key": ["B", "A", "A", "B", "C"],
            "registrar_row_index": [0, 1, 2, 3, 4],
        }
    )
    out = drop_in_file_duplicates(df)
    assert list(out["composite_key"]) == ["B", "A", "C"]
