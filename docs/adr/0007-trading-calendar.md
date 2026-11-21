# ADR 0007 — Trading calendar

- Status: Accepted
- Date: 2026-11-21

## Decision
- The `trading_calendars` table is the authority. It stores every calendar date with
  `is_trading_day`, open/close times in UTC, and provenance.
- It is seeded from the `exchange_calendars` XIDX rules (Apache-2.0). Rows from other sources
  (manual corrections) are never overwritten by a reseed.
- "EOD ready" = session close + `IDX_EOD_READY_BUFFER_MINUTES` (default 90). The scheduler works
  out the latest completed session instead of assuming it started on time (PRD §18.1).
- "Today" always means today in Asia/Jakarta (`idx_trading.clock`).

## Known limits
- The library can generate XIDX sessions back to 1990, but the rules for older years look
  incomplete (for example, December 2002 has only 13 sessions). Sessions before about 2006 must be
  checked against observed trading data before they are trusted (open issue OI-13).
- Library holiday rules must be checked against official IDX holiday announcements (OI-14).
