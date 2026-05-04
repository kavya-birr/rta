"""CAMS adapters — CSV and DBF variants sharing a common base. See spec §5 step 3."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

from openreversefeed.adapters.base import AggregationStrategy, FeedAdapter, PairRemovalStrategy
from openreversefeed.adapters.registry import default_registry
from openreversefeed.core.models import Action, Registrar

# ---------------------------------------------------------------------------
# Field maps
# ---------------------------------------------------------------------------

_CSV_FIELD_MAP: dict[str, str] = {
    "USRTRXNO": "transaction_id",
    "FOLIO_NO": "folio_number",
    "PRODCODE": "product_code",
    "SCHEME_CODE": "scheme_code",
    "UNITS": "units",
    "AMOUNT": "amount",
    "TRADDATE": "transaction_date",
    "TRXNMODE": "transaction_mode",
    "TRXNTYPE": "transaction_type",
    "TRNSERIALNO": "transaction_number",
    "NAV": "nav",
    "BROKCODE": "broker_code",
    "PANNO": "pan",
    "INVNAME": "investor_name",
    "REINVEST_F": "dividend_option_flag",
}

_DBF_FIELD_MAP: dict[str, str] = {
    "USRTRXNO": "transaction_id",
    "FOLIO_NO": "folio_number",
    "PRODCODE": "scheme_code",  # DBF uses PRODCODE as the scheme code
    "UNITS": "units",
    "AMOUNT": "amount",
    "TRADDATE": "transaction_date",
    "TRXNMODE": "transaction_mode",
    "TRXNTYPE": "transaction_type",
    "TRXNNO": "transaction_number",
    "PURPRICE": "nav",
    "BROKCODE": "broker_code",
    "PAN": "pan",
    "INV_NAME": "investor_name",
    "REINVEST_F": "dividend_option_flag",
    "AMC_CODE": "product_code",
}

# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

# Mapping from the raw CAMS REINVEST_F flag to the canonical plan_type
# vocabulary the scheme master stores. 'Y' means reinvest, 'N' means
# payout. Anything else (empty, 'P' for pure growth, nulls) → None,
# which means "the feed doesn't tell us, don't assert anything."
CAMS_REINVEST_F_TO_PLAN_TYPE: dict[str, str] = {
    "Y": "idcw_reinvest",
    "N": "idcw_payout",
}

_TYPE_FLIP_MAP: dict[str, str] = {
    "P": "R",
    "R": "P",
    "SI": "SO",
    "SO": "SI",
    "TI": "TO",
    "TO": "TI",
    "D": "DP",
    "DP": "D",
}

_BUY_TYPES = {"P", "SI", "TI", "D", "BON", "NFO"}
_SELL_TYPES = {"R", "SO", "TO", "DP"}
_NO_EFFECT_TYPES = {"N", "J"}

_TYPE_TO_TAG = {
    "P": "purchase",
    "R": "redemption",
    "SI": "switch_in",
    "SO": "switch_out",
    "TI": "transfer_in",
    "TO": "transfer_out",
    "D": "dividend",
    "DP": "dividend_payout",
    "BON": "bonus",
    "NFO": "new_fund_offer",
}

# CAMS transaction types we actively refuse. TICOB / TOCOB are the
# "close of business" variants of transfer in/out and the source system
# rejects them at validation time.
_REJECTED_TYPES = {"TICOB", "TOCOB"}

# ---------------------------------------------------------------------------
# Strategies (shared by all CAMS variants)
# ---------------------------------------------------------------------------


class _CamsPairStrategy(PairRemovalStrategy):
    def remove(self, df: pd.DataFrame) -> pd.DataFrame:
        from openreversefeed.core.pair_removal import remove_cams_pairs

        return remove_cams_pairs(df)


class _CamsAggregation(AggregationStrategy):
    def merge_partial_records(self, df: pd.DataFrame) -> pd.DataFrame:
        from openreversefeed.core.aggregation import aggregate_cams_switches

        return aggregate_cams_switches(df)


# ---------------------------------------------------------------------------
# Base class — shared parse, normalize, classify, composite_key
# ---------------------------------------------------------------------------


class _CamsBase(FeedAdapter):
    registrar = Registrar.CAMS
    type_flip_map = _TYPE_FLIP_MAP
    rejected_types = _REJECTED_TYPES

    @staticmethod
    def _strip_quotes(df: pd.DataFrame) -> pd.DataFrame:
        """Strip surrounding quotes from column names and values.

        Some CAMS CSV exports wrap every field in single-quotes.
        """
        stripped_cols = {c: c.strip("'\" ") for c in df.columns}
        if any(k != v for k, v in stripped_cols.items()):
            df = df.rename(columns=stripped_cols)
            for col in df.columns:
                if df[col].dtype == object:
                    df[col] = df[col].str.strip("'\" ")
        return df

    def parse(self, file_path: str | Path) -> pd.DataFrame:
        path = Path(file_path)
        suffix = path.suffix.lower()
        if suffix in (".xls", ".xlsx"):
            df = pd.read_excel(path, dtype=str)
        elif suffix == ".csv":
            df = pd.read_csv(path, dtype=str)
        elif suffix == ".dbf":
            from dbfread import DBF

            records = [dict(r) for r in DBF(str(path), load=True, char_decode_errors="ignore")]
            df = pd.DataFrame(records, dtype=str)
        else:
            raise ValueError(f"unsupported file type for CAMS: {suffix}")
        return self._strip_quotes(df)

    def normalize(self, raw: pd.DataFrame) -> pd.DataFrame:
        canonical_cols = {src: dst for src, dst in self.field_map.items() if src in raw.columns}
        unknown_cols = [c for c in raw.columns if c not in canonical_cols]

        df = raw.rename(columns=canonical_cols).copy()

        if unknown_cols:
            df["__source_meta"] = raw[unknown_cols].to_dict(orient="records")
        else:
            df["__source_meta"] = [{}] * len(df)

        # Translate the CAMS REINVEST_F flag into the canonical plan_type
        # vocabulary the scheme master uses.
        if "dividend_option_flag" in df.columns:
            df["plan_type_from_feed"] = (
                df["dividend_option_flag"]
                .astype(str)
                .str.strip()
                .str.upper()
                .map(CAMS_REINVEST_F_TO_PLAN_TYPE)
            )
        else:
            df["plan_type_from_feed"] = None

        df.insert(0, "registrar_row_index", range(len(df)))
        return df

    def pair_strategy(self) -> PairRemovalStrategy:
        return _CamsPairStrategy()

    def aggregation_strategy(self) -> AggregationStrategy:
        return _CamsAggregation()

    def classify_row(self, row: dict[str, Any]) -> tuple[Action, str, bool]:
        mode = row.get("transaction_mode")
        txn_type = str(row.get("transaction_type", ""))

        # Match on longest prefix (handles composite codes like SISF22S → SI)
        prefix = next(
            (
                t
                for t in sorted(_TYPE_TO_TAG.keys(), key=len, reverse=True)
                if txn_type.startswith(t)
            ),
            txn_type,
        )

        if prefix in _BUY_TYPES:
            action = Action.BUY
        elif prefix in _SELL_TYPES:
            action = Action.SELL
        elif prefix in _NO_EFFECT_TYPES:
            action = Action.NO_EFFECT
        else:
            action = Action.NO_EFFECT

        tag = _TYPE_TO_TAG.get(prefix, "other")

        if mode == "R":
            return action, "reversal", True
        return action, tag, False

    def composite_key(self, row: dict[str, Any]) -> str:
        date_val = row["transaction_date"]
        date_str = date_val.strftime("%Y%m%d") if hasattr(date_val, "strftime") else str(date_val)
        return (
            f"{row['original_trans_number']}_{row['transaction_type']}_"
            f"{row['transaction_number']}_{date_str}"
        )


# ---------------------------------------------------------------------------
# Concrete adapters
# ---------------------------------------------------------------------------


class CamsAdapter(_CamsBase):
    """CAMS CSV / XLS format with SCHEME_CODE + TRNSERIALNO columns."""

    name = "cams"
    priority = 100
    mandatory_headers = {
        "USRTRXNO",
        "FOLIO_NO",
        "PRODCODE",
        "SCHEME_CODE",
        "UNITS",
        "AMOUNT",
        "TRADDATE",
        "TRXNMODE",
        "TRXNTYPE",
    }
    discriminator_headers: set[str] = set()
    field_map = _CSV_FIELD_MAP


class CamsDbfAdapter(_CamsBase):
    """CAMS DBF format — uses TRXNNO, PURPRICE, PAN, INV_NAME instead of
    their CSV counterparts, and PRODCODE doubles as the scheme code."""

    name = "cams_dbf"
    priority = 95
    mandatory_headers = {
        "USRTRXNO",
        "FOLIO_NO",
        "PRODCODE",
        "TRXNNO",
        "UNITS",
        "AMOUNT",
        "TRADDATE",
        "TRXNMODE",
        "TRXNTYPE",
    }
    discriminator_headers = {"PURPRICE", "PAN"}
    field_map = _DBF_FIELD_MAP


default_registry.register(CamsAdapter)
default_registry.register(CamsDbfAdapter)
