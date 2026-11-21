# ADR 0002 — Instrument identity and ticker history

- Status: Accepted
- Date: 2026-11-21

## Context
IDX tickers change (rebranding, mergers), and a freed ticker can later be used by a different
company. Survivorship-free research needs delisted names (PRD §6.4, FR-002, FR-004).

## Decision
- `instruments.instrument_id` is a permanent surrogate key. Tickers are **not** identity.
- `ticker_history` stores half-open intervals `[valid_from, valid_to)`. Resolving a ticker always
  needs a date: `resolve(ticker, on)`.
- A ticker change closes the open interval and opens a new one for the **same** instrument, so
  the price history stays in one piece.
- Overlaps are refused: one ticker cannot belong to two instruments at once, and one instrument
  cannot have two tickers at once.
- A `delisted` event closes the instrument's open ticker interval on the delisting date. Prices
  reported for that code afterwards are rejected as `unknown_ticker`, and the code can be reused
  by a new issuer. A relisting must assign a ticker again.
- Listing status (`active`/`suspended`/`delisted`) is not stored as a mutable flag. It is worked
  out by replaying `listing_history` events up to the date, so any past date can be rebuilt.
- Base universe eligibility = common stock AND actively listed on that date. Liquidity filtering
  (PRD §6.5) will be added on top later and is configurable.
- If the listing date is unknown, the first ticker interval starts at 1977-08-10 (first JSX
  listing), and its `reason` says so.

## Consequences
- Every price row, signal and backtest trade points to `instrument_id`, never to a ticker string.
- Importers must give dates for ticker changes; undated changes cannot be recorded.
