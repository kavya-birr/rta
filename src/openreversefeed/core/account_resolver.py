"""Account resolver handling single and family PAN cases."""
from __future__ import annotations

from typing import Any

from openreversefeed.core.cache import PrewarmCache


class AmbiguousPanError(Exception):
    def __init__(self, pan: str, candidate_ids: list[str]) -> None:
        self.pan = pan
        self.candidate_ids = candidate_ids
        super().__init__(f"family PAN {pan} ambiguous: {candidate_ids}")


class PanNotFoundError(Exception):
    pass


def resolve_account(row: dict[str, Any], cache: PrewarmCache) -> dict[str, Any]:
    pan = str(row["pan"]).strip().upper()
    by_ownership = cache.accounts_by_pan.get(pan)
    if not by_ownership:
        raise PanNotFoundError(pan)

    # Single account
    if len(by_ownership) == 1:
        return next(iter(by_ownership.values()))

    # Explicit ownership match
    ot = row.get("ownership_type")
    if ot and ot in by_ownership:
        return by_ownership[ot]

    # Prefer 'individual' default
    if "individual" in by_ownership:
        return by_ownership["individual"]

    raise AmbiguousPanError(pan, [acc["id"] for acc in by_ownership.values()])
