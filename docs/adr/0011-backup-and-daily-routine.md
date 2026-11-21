# ADR 0011 — Backups, restore tests and the daily routine

- Status: Accepted
- Date: 2026-11-21
- Implements: PRD §8.1, §18.1–18.3

## Decision

### Backups (`ops/backup.py`, `idx backup …`)
- **PostgreSQL:** `pg_dump -Fc` on an exported snapshot. The manifest's row counts are taken
  in the same REPEATABLE READ transaction, so they describe exactly the dumped data.
  **SQLite:** the online backup API.
- Each backup has a manifest with SHA-256, size, Alembic revision, per-table row counts and the
  tool version.
- **Restore test** (`idx backup verify`): checksum, then a full `pg_restore` into a scratch
  database (created and dropped; never the live one), with row counts and the schema revision
  compared. The result is stored next to the backup.
- **Retention:** 90 days (`IDX_BACKUP_KEEP_DAYS`); the newest backup is never deleted.
- **Raw archive copy** (`idx backup archive`): incremental. Every data file is checked against
  its SHA-256 file name. A damaged copy is repaired; a damaged source is reported and not copied.
- **Status** (`idx backup status`):
  - healthy = a backup less than 26 h old and a passed restore test less than 35 days old;
  - degraded = backups exist but one of those conditions fails;
  - failed = no backup can be shown to restore.
- Backups live in `IDX_BACKUP_DIR` (default `./backups`, git-ignored). PRD §18.2 asks for
  *separate* storage, so point it at another disk or a synced folder.
- The database dump includes `artifact_fetches`, so the fetch log is covered (closes OI-24
  when backups run).

### Daily routine (`idx daily`)
- Steps: registry sync → EOD prices (EODHD, within budget) → corporate actions (EODHD) →
  factors → vendor reconciliation → cross-source reconciliation → freshness → backup →
  archive copy → prune → restore test (when the last passed one is older than 30 days).
- Each step is isolated. Optional steps degrade the run; critical ones (registry, prices,
  backup, archive copy) fail it. Exit code 0/1/2 = healthy/degraded/failed (PRD §18.3).
- The free EODHD budget is split by default into 16 calls for prices and 4 for corporate
  actions (2 tickers).
- Data collection covers every listed common stock (active, suspended, or unknown listing
  history). BUY eligibility is a separate, stricter filter (bug found while building this:
  collection had used BUY eligibility).

## Verification (live)
- Backup of the live PostgreSQL database (3,062 rows, revision 0003) → restore into a scratch
  database → all counts and the revision match.
- Archive copy: 26 files, then 0 copied and 26 verified on the second run.
- `idx daily` with a zero API budget reports `degraded` because prices were skipped; every
  other step passed.
- Bug found: `with sqlite3.connect()` does not close connections. Fixed with `closing()`.
