# ADR 0001 — Architecture and repository layout

- Status: Accepted
- Date: 2026-11-21

## Context
The PRD (§17, §28, §30) asks for a modular, testable, CPU-light Python platform that starts with
data infrastructure and runs both locally (Windows) and in GitHub Actions.

## Decision
- A single Python package `idx_trading` in a `src/` layout, organised by domain:
  `registry` (data sources), `instruments` (master), `calendar`, `ingestion` (adapters → archive →
  validation → writer → pipeline), `db` (models, sessions), `cli`.
- A modular monolith, not services: one person runs it, and a monolith keeps it simple and portable.
- Dependency management with `uv`; `uv.lock` pins exact versions (PRD §17.3). Supported Python
  is 3.11–3.13; CI runs the locked set.
- Schema changes only through Alembic migrations (`migrations/`). A test fails if the ORM models
  and the migrations drift apart.
- Heavy analytics dependencies (polars, duckdb, pyarrow) are an optional `analytics` extra and are
  not needed for Phase 1.
- Settings come from `IDX_*` environment variables or `.env` (never committed).

## Consequences
- Each layer can be tested on its own; adapters cannot write to the database directly.
- Supabase sync, feature store and models will be added as new packages, not rewrites.
