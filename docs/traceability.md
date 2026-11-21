# Requirements Traceability Matrix

Status legend: **Done** = implemented and tested · **Partial** = foundation in place, more work
scheduled · **Schema** = tables and migration only · **Planned** = not started.
Milestone numbering is described in `implementation-log.md`.

## Functional requirements

| ID | Requirement | Status | Where | Verified by |
|---|---|---|---|---|
| FR-001 | BEI instrument master | Partial | `instruments/service.py`, `instruments/importer.py` | `tests/unit/test_instruments.py` |
| FR-002 | Ticker history | Done | `ticker_history`, `InstrumentService.assign_ticker/change_ticker/resolve` | `test_ticker_change_preserves_identity`, `test_ticker_reuse_*`, `test_overlapping_*` |
| FR-003 | EOD OHLCV ingestion | Partial — CSV adapter + EODHD adapter (per-ticker on free plan, bulk on paid); live JK test passed 2026-11-21 | `ingestion/`, `ingestion/adapters/eodhd.py` | `tests/integration/test_pipeline.py`, `tests/integration/test_eodhd_pipeline.py`, `tests/unit/test_eodhd_adapter.py` |
| FR-004 | Delisted historical data | Partial — modelled and importable; real data depends on OI-02 | `listing_history`, `status_on` | `test_csv_import_is_idempotent_and_keeps_delisted`, `test_listing_status_replay` |
| FR-005 | Corporate action management | Schema | `db/models/corporate_actions.py` | migration test |
| FR-006 | Financial statement ingestion | Schema (point-in-time columns) | `db/models/fundamentals.py` | migration test |
| FR-007 | Financial ratio computation | Planned (M2) | — | — |
| FR-008 | Data provenance | Done for prices | `raw_artifacts`, `ingestion_runs`, `ingestion_records`, FK on `daily_prices` | `test_end_to_end_csv_ingestion` |
| FR-011 | Data cleaning | Partial (no-trade normalisation only, by design) | `validation.normalize_no_trade` | `test_no_trade_normalization` |
| FR-012 | Duplicate detection | Done | `validation.dedupe_batch`, record hashing, unique indexes | `test_dedupe_batch`, `test_rerun_is_idempotent`, `test_conflicting_duplicates_*` |
| FR-013 | Missing data detection | Done for sessions | `ingestion/coverage.py` | `test_missing_sessions_and_freshness` |
| FR-018 | Point-in-time feature store | Planned (M2); PIT columns ready | `financial_reports.available_at`, bitemporal prices | — |
| FR-042 | Immutable signals | Planned; same versioning pattern as ADR 0003 | — | — |
| FR-049 | Pipeline monitoring | Partial — run log + CLI | `ingestion_runs`, `idx ingest runs/freshness` | `test_cli_end_to_end` |
| FR-052 | Daily report | Planned | — | — |
| All other FRs | Phase 2+ | Planned | — | — |

## PRD §8.2 data quality rules

| Rule | Code | Severity |
|---|---|---|
| Duplicate detection | `duplicate_conflict` / `duplicate_identical` | error / info |
| Missing observations | `coverage.missing_sessions` | report |
| Invalid ticker | `unknown_ticker` | error |
| Negative volume | `negative_quantity` | error |
| Invalid OHLC relationships | `ohlc_inconsistent`, `price_non_positive`, `close_missing` | error |
| Extreme price changes | `extreme_move` | warning |
| Corporate action inconsistencies | `extreme_move` as a proxy; full check needs M1.3 | warning |
| Stale data | `coverage.check_freshness` | report |
| Cross-source discrepancies | rows kept per source; reconciliation job planned (M1.5) | — |
| Revision of stored data | `revision_conflict` | warning |

## Non-functional requirements

| ID | Status | Evidence |
|---|---|---|
| NFR-002 Data integrity | Done (Phase 1 scope) | CHECK/unique constraints, atomic run transactions, `test_adapter_crash_records_failed_run_and_rolls_back` |
| NFR-003 Data quality observability | Partial | `data_quality_issues`, run counters |
| NFR-004 Reproducibility | Partial | `uv.lock`, versioned prices, content-addressed archive, `code_version` on runs, `idx ingest replay` reproduces identical rows (`tests/integration/test_replay.py`) |
| NFR-007 Prediction traceability | Foundation | every price row → run → raw artifact |
| NFR-009 Modular code | Done | ADR 0001 |
| NFR-010 Local/cloud portability | Partial | SQLite + PostgreSQL test matrix, env-based config |
| NFR-012 CPU efficiency | Done (Phase 1) | no heavy dependencies at runtime |
| NFR-013 Legal data acquisition | Done | ADR 0004, ADR 0008 (source review), `test_unpermitted_source_never_runs`, `test_shipped_registry_reflects_source_review` |
| NFR-005/006 Secrets | Done for EODHD | token only from env, redacted in URLs, archive and run params (`test_eodhd_end_to_end_is_idempotent`) |
| NFR-015 No broker execution | Done | no execution code exists |
