# ADR 0006 — Testing strategy

- Status: Accepted
- Date: 2026-11-21

## Decision
- Every database test runs on SQLite (no setup needed, works on Windows). When
  `IDX_TEST_DATABASE_URL` is set, the same tests also run on PostgreSQL; CI always sets it.
- A migration test applies `alembic upgrade head`, checks that the result has no differences from
  the ORM metadata, and downgrades back to empty, on both backends.
- `pytest` treats warnings as errors. Only a known upstream deprecation inside
  `exchange_calendars` is ignored.
- The fixtures are synthetic and labelled as such (`tests/fixtures`). They are not market data and
  must never be loaded into a real database.
