# ADR 0003 — Bitemporal, versioned price storage

- Status: Accepted
- Date: 2026-11-21

## Context
Sources can restate data. The PRD requires idempotent ingestion, no overwriting of valid history,
cross-source reconciliation and point-in-time reproducibility (§7.3, §8.2, §17.4).

## Decision
- `daily_prices` holds **raw** (unadjusted) bars. Split-adjusted and total-return series will be
  derived from versioned `price_adjustment_factors`, never stored in place of raw data (§7.4).
- Rows are kept **per source**: the key is `(instrument_id, trade_date, source_id)`. This lets
  reconciliation compare sources without losing either one.
- Each row has a `record_hash` of its value fields, with decimals normalised so that 1500 and
  1500.00 hash the same. Re-ingesting the same values gives `unchanged`.
- Different values for an existing key are handled by a `RevisionPolicy`:
  - `flag` (default): keep the stored row, record a `revision_conflict` issue with both versions.
  - `supersede` (explicit opt-in, `--supersede`): set the old row `is_current=false`,
    `superseded_at=now`, and insert `version+1`.
- A partial unique index allows exactly one current row per key.
- Every row links to its `ingestion_run_id` and the `artifact_id` of the raw file it came from.

## Consequences
- Nothing is ever silently overwritten. Every number can be traced back to its source file.
- Queries for "current" data must filter on `is_current`. A view or helper will be added once
  consumers exist.
