# ADR 0005 — Data-quality severity model

- Status: Accepted
- Date: 2026-11-21

## Decision
- Rules return findings with severity `error`, `warning` or `info` (`ingestion/validation.py`).
- Any `error` → record `invalid` → **rejected**. It is not written to `daily_prices`, but it is
  kept in `ingestion_records` and `data_quality_issues`. Errors: missing close, non-positive price,
  inconsistent OHLC, negative quantity, future date, unknown ticker, non-trading day, parse error,
  conflicting duplicates in one batch.
- `warning` → stored with `validation_status='warning'`, and an issue is opened. Warnings:
  extreme move against the reference close (default threshold 35%, configurable, may be a
  corporate action), volume/value disagreement, implied VWAP outside the day's range, trading
  reported for a suspended or delisted instrument, date missing from the calendar, revision
  conflict.
- `info` → recorded on the ingestion record only, for example identical duplicates collapsed or
  no-trade normalisation.
- **No-trade normalisation** is the only change made to raw values: when volume is 0, zero
  open/high/low values are stored as NULL and the reference close is kept.
- Run status: `succeeded` (no rejections), `partial` (some rejected, some accepted), `failed`
  (exception, no records, or everything rejected).
- Later stages must treat `invalid`/missing bars as "block recommendation" (PRD §8.3).
