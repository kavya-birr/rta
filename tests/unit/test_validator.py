import pytest

from openreversefeed.core.cache import PrewarmCache
from openreversefeed.core.models import CorrectionType
from openreversefeed.core.validator import ValidationError, validate_row

_CACHE = PrewarmCache(
    schemes_by_code={"SCH1": {"id": 1, "amc_id": 10}},
    accounts_by_pan={"ABCDE1234F": {"individual": {"id": "acc-1"}}},
)

_VALID = {
    "pan": "ABCDE1234F",
    "folio_number": "F1",
    "scheme_code": "SCH1",
    "amount": 1000.0,
    "units": 10.0,
    "transaction_date": "2025-01-01",
}


def test_valid_row_passes():
    validate_row(dict(_VALID), _CACHE)


def test_missing_pan_raises_pan_not_found():
    row = dict(_VALID)
    row["pan"] = ""
    with pytest.raises(ValidationError) as exc_info:
        validate_row(row, _CACHE)
    assert exc_info.value.correction_type is CorrectionType.OTHER


def test_invalid_pan_format_raises():
    row = dict(_VALID)
    row["pan"] = "NOTAPAN"
    with pytest.raises(ValidationError) as exc_info:
        validate_row(row, _CACHE)
    assert exc_info.value.correction_type is CorrectionType.PAN_NOT_FOUND


def test_unknown_pan_in_cache_raises():
    row = dict(_VALID)
    row["pan"] = "ZZZZZ9999Z"
    with pytest.raises(ValidationError) as exc_info:
        validate_row(row, _CACHE)
    assert exc_info.value.correction_type is CorrectionType.PAN_NOT_FOUND


def test_unknown_scheme_raises():
    row = dict(_VALID)
    row["scheme_code"] = "UNKNOWN"
    with pytest.raises(ValidationError) as exc_info:
        validate_row(row, _CACHE)
    assert exc_info.value.correction_type is CorrectionType.SCHEME_NOT_FOUND


def test_missing_folio_number():
    row = dict(_VALID)
    row["folio_number"] = ""
    with pytest.raises(ValidationError) as exc_info:
        validate_row(row, _CACHE)
    assert exc_info.value.correction_type is CorrectionType.OTHER
    assert "folio_number" in exc_info.value.message
