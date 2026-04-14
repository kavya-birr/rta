"""Per-row validator. See spec §5 step 8a."""
from __future__ import annotations

import re
from typing import Any

from openreversefeed.core.cache import PrewarmCache
from openreversefeed.core.models import CorrectionType

_PAN_REGEX = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")

_REQUIRED_FIELDS = [
    "pan",
    "folio_number",
    "scheme_code",
    "amount",
    "units",
    "transaction_date",
]


class ValidationError(Exception):
    def __init__(self, correction_type: CorrectionType, message: str) -> None:
        self.correction_type = correction_type
        self.message = message
        super().__init__(f"{correction_type.value}: {message}")


def validate_row(row: dict[str, Any], cache: PrewarmCache) -> None:
    # Required fields present (treat None, empty string, and NaN as missing)
    for f in _REQUIRED_FIELDS:
        val = row.get(f)
        if val is None or val == "" or (isinstance(val, float) and val != val):
            raise ValidationError(CorrectionType.OTHER, f"missing required field: {f}")

    pan = str(row["pan"]).strip().upper()
    if not _PAN_REGEX.match(pan):
        raise ValidationError(CorrectionType.PAN_NOT_FOUND, f"invalid PAN format: {pan}")
    if pan not in cache.accounts_by_pan:
        raise ValidationError(CorrectionType.PAN_NOT_FOUND, f"PAN not in accounts: {pan}")

    if row["scheme_code"] not in cache.schemes_by_code:
        raise ValidationError(
            CorrectionType.SCHEME_NOT_FOUND, f"scheme not found: {row['scheme_code']}"
        )
