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
| FR-005 | Corporate action management | Done for splits, dividends, rights and bonus shares (ADR 0009); history limited by source (OI-26) | `corporate_actions/` | `tests/unit/test_factors.py`, `tests/integration/test_corporate_actions.py` |
| FR-006 | Financial statement ingestion | Partial — banks via the OJK publication-report parser; non-banks open (OI-29) | `fundamentals/` | `tests/unit/test_bank_publication.py`, `tests/integration/test_fundamentals.py` |
| FR-007 | Financial ratio computation | Done for the reported concepts (PER, PBV, ROE, ROA, DER, LDR, growth) | `fundamentals/ratios.py` | `test_ttm_and_ratios`, `test_cli_fin_commands` |
| FR-008 | Data provenance | Done for prices | `raw_artifacts`, `ingestion_runs`, `ingestion_records`, FK on `daily_prices` | `test_end_to_end_csv_ingestion` |
| FR-011 | Data cleaning | Partial (no-trade normalisation only, by design) | `validation.normalize_no_trade` | `test_no_trade_normalization` |
| FR-012 | Duplicate detection | Done | `validation.dedupe_batch`, record hashing, unique indexes | `test_dedupe_batch`, `test_rerun_is_idempotent`, `test_conflicting_duplicates_*` |
| FR-013 | Missing data detection | Done for sessions | `ingestion/coverage.py` | `test_missing_sessions_and_freshness` |
| FR-014 | Technical features | Done (`tech_v1`, ADR 0013) | `features/technical.py`, `features/store.py` | `tests/unit/test_features.py` |
| FR-018 | Point-in-time feature store | Done for technical features (as_of build, definition hash) and fundamentals queries | `features/store.py`, `fundamentals/pit.py` | `test_republication_is_a_revision_and_restatement_is_flagged`, feature look-ahead test |
| FR-019 | Baseline models | Done (momentum rank, ridge, logistic; ADR 0015) | `models/estimators.py` | `test_report_is_reproducible_and_recovers_a_planted_signal` |
| FR-020 | ML model competition | Done (LightGBM classifier/regressor vs baselines, same folds) | `models/evaluate.py` | same |
| FR-021/022/024 | Multi-horizon targets, expected return, downside | Partial — labels (ADR 0014); predictions per horizon via `idx models evaluate --horizon` | `features/labels.py` | `tests/unit/test_labels.py` |
| FR-023 | Calibrated probability | Partial — Brier, ECE, reliability measured; calibration step not yet applied | `models/metrics.py` | `test_calibration_metrics` |
| FR-026 | Model registry | Done (candidate → manual approval → retired; hash-verified artifacts) | `models/registry.py`, `model_versions` | `test_approval_is_manual_and_single_per_horizon`, `test_tampered_artifact_is_refused` |
| FR-038 | Walk-forward validation | Done (expanding window, purge by label exit date, embargo) | `models/walkforward.py` | `test_split_purges_*`, `test_no_fold_uses_the_future` |
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
| Corporate action inconsistencies | `corporate_action_inconsistency` (split vs price), `ca_*` input rules, `adjustment_unresolved` | warning / error |
| Stale data | `coverage.check_freshness` | report |
| Cross-source discrepancies | `cross_source_price_mismatch` (prices exact, volume ±5% after basis conversion), `idx ingest reconcile` | error / warning |
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

## PRD §7.4 / §22 (Data) acceptance

| Criterion | Status | Evidence |
|---|---|---|
| Three price representations (raw, split-adjusted, total-return) | Done | `corporate_actions/adjusted.py`, `idx prices show --adjust` |
| Versioned adjustment factors with history | Done | `price_adjustment_factors.factor_version`, `test_revision_creates_new_factor_version` |
| Raw and adjusted prices can be reconciled | Done | `idx ca reconcile`; live: 5/5 pass, max 0.000002% |
| No look-ahead in adjustment | Done | `as_of`, `test_cumulative_applies_only_to_bars_before_ex_date_and_respects_as_of` |
| Fundamental publication timestamps preserved (PRD §22) | Done | `published_at` + basis, `available_at` = next day; revisions kept |
