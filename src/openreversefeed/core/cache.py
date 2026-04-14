"""PrewarmCache — batched lookups populated once per file."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class PrewarmCache:
    schemes_by_code: dict[str, dict[str, Any]] = field(default_factory=dict)
    accounts_by_pan: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)
    folios_by_account_folio_amc: dict[tuple[str, str, int], dict[str, Any]] = field(
        default_factory=dict
    )
    transactions_by_composite_key: dict[tuple[str, int, str], dict[str, Any]] = field(
        default_factory=dict
    )
    transactions_by_registrar_txn_id: dict[tuple[str, int, str], dict[str, Any]] = field(
        default_factory=dict
    )
    processing_records_by_composite_key: dict[str, dict[str, Any]] = field(default_factory=dict)
