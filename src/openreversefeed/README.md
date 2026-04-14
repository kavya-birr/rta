# src/openreversefeed/ — library internals

This is a walkthrough of **how** the library actually turns a raw CAMS or
KFintech feed file into ledger rows. It sits between the quickstart in the
top-level `README.md` and the source itself. If you're skimming: read this.
If you're debugging: read this **and** the cited files side-by-side.

---

## Ten-step pipeline

```
file on disk
    │
    ▼  (1) storage.fetch         — local path or s3://
    │
    ▼  (2) AdapterRegistry.detect — pick an adapter by headers
    │
    ▼  (3) adapter.parse          — CSV / XLS / XLSX / DBF → raw DataFrame
    │
    ▼  (4) adapter.normalize      — rename to canonical columns + __source_meta
    │
    ▼  (5) cleaner.run(df, adapter)
    │         pair_removal    (registrar-specific strategy)
    │         negative_fix    (flip signs + flip types)
    │         aggregation     (merge partial transfer / switch rows)
    │         conflict        (KFintech P+SIN dedup)
    │         composite_key   (deterministic string per row)
    │         dedup           (drop in-file duplicates on composite_key)
    │         classify        (action + action_tag + is_reversal)
    │
    ▼  (6) prewarm — 5 batch SELECTs, no N+1
    │
    ▼  (7) per-row loop
    │         validate          (required fields, PAN regex, scheme exists)
    │         resolve account   (single PAN / family PAN / correction)
    │         upsert transaction (ON CONFLICT on composite_key)
    │         emit outbox event (same DB txn)
    │         log processing_record
    │
    ▼  (8) recompute positions (FIFO) for affected (account, folio, scheme)
    │
    ▼  (9) drain outbox worker publishes events (at-least-once)
    │
    ▼ (10) ingestion_runs row updated with final stats
```

The key property of the whole pipeline is that **everything in the cleaner
step (5) is a pure function on a DataFrame**. The same input always produces
the same output regardless of row order, pandas engine, or Python version,
because every groupby is preceded by an explicit deterministic sort.

---

## CAMS logic — where and how

**File:** [`adapters/cams.py`](adapters/cams.py)

### Detection
`CamsAdapter.mandatory_headers` is the set of columns every CAMS file must
have:

```
{USRTRXNO, FOLIO_NO, PRODCODE, SCHEME_CODE, UNITS, AMOUNT,
 TRADDATE, TRXNMODE, TRXNTYPE}
```

If the registry finds all of those, it picks CAMS (priority 100, which is
the highest of all shipped adapters).

### Field map → canonical columns
The adapter renames source columns into the canonical set the rest of the
library speaks:

```
USRTRXNO    → transaction_id          (the real BSE order id)
FOLIO_NO    → folio_number
PRODCODE    → product_code
SCHEME_CODE → scheme_code
UNITS       → units
AMOUNT      → amount
TRADDATE    → transaction_date
TRXNMODE    → transaction_mode        (N = normal, M = modified, R = reversal)
TRXNTYPE    → transaction_type        (P/R/SI/SO/TI/TO/D/DP/BON/N/J)
TRNSERIALNO → transaction_number
NAV         → nav
BROKCODE    → broker_code
PANNO       → pan
INVNAME     → investor_name
```

Any column CAMS sends that we don't recognise gets stashed in a
`__source_meta` dict column so it survives round-trip audits.

### Classification (`CamsAdapter.classify_row`)
CAMS puts the business intent directly in `transaction_type`, so the
classifier is a prefix lookup:

| Prefix | Action | Tag |
|---|---|---|
| `P`   | buy  | purchase |
| `R`   | sell | redemption |
| `SI`  | buy  | switch_in |
| `SO`  | sell | switch_out |
| `TI`  | buy  | transfer_in |
| `TO`  | sell | transfer_out |
| `D`   | buy  | dividend |
| `DP`  | sell | dividend_payout |
| `BON` | buy  | bonus |
| `N`, `J` | no_effect | other |

Longest-match prefix wins, so composite codes like `SISF22S` classify as
`SI → switch_in`.

Any row (regardless of type) with `transaction_mode='R'` gets its tag
overridden to `reversal` and `is_reversal=True`. That is the single source
of truth for "this is a reversal" — everything downstream reads it from
there.

### Pair removal — CAMS has no parent link
CAMS doesn't ship a `parent_transaction_number`, so the CAMS pair-removal
strategy works differently from KFintech. See
[`core/pair_removal.py`](core/pair_removal.py) → `remove_cams_pairs`:

1. Group rows by `(folio_number, transaction_type, transaction_number)`.
2. In each group, look for **originals** (mode in `M` or `R`, units > 0)
   and **reversals** (mode `R`, units < 0).
3. Self-merge by the group key and keep pairs where `units` and `amount`
   are opposite within 1e-6 tolerance.
4. Drop both sides.

The merge is vectorized (one pandas op, no row loops) so even 100k-row
files finish in milliseconds.

### Aggregation — switches only
CAMS ships partial switch rows (SI / SO) that need to be collapsed into a
single "business transaction" row. Transfers (TI / TO) are already
pre-aggregated on CAMS's side. See
[`core/aggregation.py`](core/aggregation.py) → `aggregate_cams_switches`.

The groupby key is
`(transaction_id, transaction_type, transaction_number, transaction_date)`.
Before grouping, rows are stable-sorted by `(transaction_date,
registrar_row_index)` so that `first()` on non-summed columns is
deterministic under any input order. The property test
`tests/unit/test_determinism.py` shuffles inputs with random seeds and
asserts identical outputs.

### Composite key — `CamsAdapter.composite_key`
```
{original_trans_number}_{transaction_type}_{transaction_number}_{yyyymmdd}
```

Example: `CAMS10000006_SI_T2000006_20250119`.

This is the row's *identity* everywhere in the database. It's enforced as
`UNIQUE (registrar, amc_id, composite_key)` in the `transactions` table,
so upserts use `ON CONFLICT ... DO UPDATE WHERE ... IS DISTINCT FROM` and
we get `new / updated / noop_duplicate` classification for free.

---

## KFintech / KARVY logic — where and how

**File:** [`adapters/kfintech.py`](adapters/kfintech.py)

KFintech (the registrar formerly known as KARVY until the 2022 rebrand)
sends three sub-formats. Each is its own adapter class sharing a common
base:

| Class | Priority | Discriminator | Mandatory headers |
|---|---|---|---|
| `KFintechFormat1Adapter` | 90 | `{TD_PURRED, TRFLAG}` | `INWARDNUM0, TD_TRNO, TD_ACNO, FMCODE, TD_UNITS, TD_AMT, TRNMODE` |
| `KFintechFormat2Adapter` | 80 | — | `INWARDNO, TD_ACNO, TD_UNITS, TD_NAV` |
| `KFintechCsvAdapter` | 70 | — | `Inward Number, Folio Number, Units, NAV` |

### The priority trick
All three formats can look similar header-wise. Format1 has a
discriminator set — it only matches if `TD_PURRED` *and* `TRFLAG` (or at
least one of them) is present. That prevents Format2 from stealing the
match when the file is actually a Format1.

The registry ranks candidates highest-priority-first, so Format1 always
wins when headers match multiple formats.

### Field map (Format1)
```
INWARDNUM0 → transaction_id              (real BSE order id)
TD_TRNO    → transaction_number          (KFintech-internal line number)
TD_PTRNO   → parent_transaction_number   (links reversal → original)
TD_ACNO    → folio_number
FMCODE     → product_code
SCHPLN     → scheme_code
TD_UNITS   → units
TD_AMT     → amount
TD_NAV     → nav
TD_TRDT    → transaction_date
TRNMODE    → transaction_mode            (N / R / M)
TD_PURRED  → transaction_purred          (P / R / D / DP)
TRFLAG     → transaction_flag            (TI / TO / SI / SO / empty)
TD_TRTYPE  → transaction_type
PANNO      → pan
INV_NAME   → investor_name
BROK_CODE  → broker_code
```

Format2 is the same with `INWARDNO` instead of `INWARDNUM0`.
CSV variant is the same fields with English column names.

### Classification — TRFLAG takes precedence over TD_PURRED
This is the single most important KFintech rule. Buy/sell intent comes
from *two* columns, and `transaction_flag` wins:

```python
if transaction_flag in {"TI", "TO", "SI", "SO"}:
    action, tag = _FLAG_TO_ACTION[flag]          # transfer_in / transfer_out / switch_in / switch_out
elif transaction_purred in {"P", "R", "D", "DP"}:
    action, tag = _PURRED_TO_ACTION[purred]      # purchase / redemption / dividend / dividend_payout
else:
    action, tag = Action.NO_EFFECT, "other"

if transaction_mode == "R":
    return action, "reversal", True              # keep the underlying action, mark as reversal
```

Why: a row can legitimately have `transaction_purred='P'` but be a
transfer out. TRFLAG is the operational source of truth in that case.

### Pair removal — the parent link is our friend
KFintech ships `TD_PTRNO` (parent transaction number) on reversals, so
we can match reversals to originals directly:

1. Filter rows to `transaction_mode='R'` with non-empty parent.
2. Self-merge each such row against every `(mode in M,R)` row where
   `transaction_number == parent_transaction_number` and
   `folio_number` matches.
3. Keep only the matches whose units and amounts are opposite within
   1e-6.
4. Drop both sides.

Edge case **the source system used to get wrong and we now handle
cleanly**: a KFintech reversal sometimes arrives with a *different*
internal transaction number than the original but the same BSE order id.
The pipeline handles this via the fallback lookup in `db/repo.py` (see
spec §5 step 8c, fallback 3): it searches for a parent by
`(registrar, amc_id, registrar_transaction_number)`, validates folio +
opposite units, and links via `parent_transaction_id` — the parent's
`composite_key` is **never rewritten**, so the audit trail stays intact.

### Aggregation — transfers and switches
See [`core/aggregation.py`](core/aggregation.py) →
`aggregate_kfintech_transfers`.

Filter to rows with `transaction_flag ∈ {TI, SI, SO, TO}`. Stable-sort by
`(transaction_date, registrar_row_index)`. Group by:

```
(transaction_purred, transaction_number, parent_transaction_number,
 folio_number, transaction_type, transaction_date)
```

Sum `units` and `amount`; take `first` on everything else. Before the
composite key overwrites `transaction_id`, we copy the original value
into `original_trans_number` so the fallback lookup in step 8c can still
find rows by the real BSE order id.

### P+SIN conflict resolution
See [`core/conflict.py`](core/conflict.py) →
`resolve_kfintech_conflicts`.

Some KFintech files ship *both* a plain `P` (purchase) row and a `SIN`
(systematic investment marker) row for the same underlying transaction.
We group by `(transaction_number, folio_number)` and, if a group contains
both `P` and `SIN`, we keep only the `P` row. Different folios with the
same transaction number are legitimate distinct orders and are left
alone.

### Composite key — `_KFintechBase.composite_key`
```
{transaction_number}_{parent_transaction_number or 0}_{folio_number}_{yyyymmdd}
```

Example: `1227_0_91046479506_20200708`.

---

## Shared core — what every adapter delegates to

Every adapter plugs into the same set of pure functions in `core/`:

| Concern | File | Function |
|---|---|---|
| Deterministic composite key | `core/composite_key.py` | `build_cams_key`, `build_kfintech_key`, `assign_composite_keys` |
| In-file duplicate drop | `core/dedup.py` | `drop_in_file_duplicates` |
| Pair removal | `core/pair_removal.py` | `remove_cams_pairs`, `remove_kfintech_pairs` |
| Negative sign flip | `core/negative_fix.py` | `correct_negative_rows` |
| Aggregation | `core/aggregation.py` | `aggregate_cams_switches`, `aggregate_kfintech_transfers` |
| KFintech conflict | `core/conflict.py` | `resolve_kfintech_conflicts` |
| Cleaner composition | `core/cleaner.py` | `Cleaner.run(df, adapter)` |
| Per-row validation | `core/validator.py` | `validate_row` |
| Family PAN resolution | `core/account_resolver.py` | `resolve_account` |
| FIFO cost basis | `core/fifo.py` | `compute_fifo` |
| Batched lookups | `core/cache.py` | `PrewarmCache` |

This is the "clean core, pluggable edges" split from the top-level README.
The adapters hold the *policy* (what does `TRFLAG='TI'` mean?); the core
holds the *mechanism* (how do we stably aggregate partial rows?).

---

## Writing a new registrar adapter

A third-party adapter is a single class:

```python
from openreversefeed.adapters.base import FeedAdapter
from openreversefeed.adapters.registry import default_registry
from openreversefeed.core.models import Action, Registrar

class MyAdapter(FeedAdapter):
    name = "my_provider"
    registrar = Registrar.CAMS           # or KFINTECH
    priority = 50                         # higher wins detection ties
    mandatory_headers = {"COL_A", "COL_B"}
    discriminator_headers: set[str] = set()  # optional: at least one must be present
    field_map = {"COL_A": "transaction_id", "COL_B": "units", ...}
    type_flip_map = {"P": "R", "R": "P"}

    def parse(self, file_path): ...
    def normalize(self, raw): ...
    def pair_strategy(self): ...
    def aggregation_strategy(self): ...
    def classify_row(self, row): return (Action.BUY, "purchase", False)
    def composite_key(self, row): return f"{row['transaction_id']}"

default_registry.register(MyAdapter)
```

Call `ReverseFeedService.process_file()` with `registrar='auto'` and
detection picks it up. No other wiring needed.

---

## What's **not** in here

- **Outbox publishers** (noop / webhook / SQS / Kafka) are scaffolded but
  still need delivery tests.
- **ReverseFeedService facade** end-to-end integration tests are still on
  the todo list — right now the Django bridge and
  `tools/end_to_end_demo.py` exercise the full pipeline manually.
- **The `orf` CLI** has stub commands; the real `migrate`, `process`,
  `outbox drain` commands are pending.

See the issue tracker for the current state.
