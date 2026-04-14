"""Canonical enums shared across the codebase."""
from __future__ import annotations

from enum import StrEnum


class Registrar(StrEnum):
    CAMS = "cams"
    KFINTECH = "kfintech"


class Action(StrEnum):
    BUY = "buy"
    SELL = "sell"
    NO_EFFECT = "no_effect"


class TransactionStatus(StrEnum):
    PENDING = "pending"
    SUCCESSFUL = "successful"
    REVERSED = "reversed"
    FAILED = "failed"


class CorrectionType(StrEnum):
    DUPLICATE_PAN = "duplicate_pan"
    PAN_NOT_FOUND = "pan_not_found"
    USER_MISMATCH = "user_mismatch"
    FOLIO_MISMATCH = "folio_mismatch"
    SCHEME_NOT_FOUND = "scheme_not_found"
    TRANSFER_IN_UNMATCHED = "transfer_in_unmatched"
    OTHER = "other"
