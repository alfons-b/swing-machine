# ADR 0009 — Corporate actions, adjustment factors and adjusted series

- Status: Accepted
- Date: 2026-11-21
- Implements: FR-005, PRD §7.4 (three price representations), §8.3 (unprocessed corporate action → hold)

## Context
PRD §7.4 requires raw, split-adjusted and total-return-adjusted prices, with factors that are
versioned and kept over time, and backtests that use prices that could really have been traded.
The budget is Rp0 (owner decision, 2026-11-21).

## Decision

### Sources and precedence
- **EODHD** `/div` and `/splits` (1 API call each per ticker, free plan = past year). Dividends
  use `unadjustedValue`, the amount actually paid per share on the day. EODHD's `value` is
  restated for later splits and is never used. Split strings are `new/old`.
- **Manual CSV** (`idx ca import`) for events EODHD does not carry or may mislabel (rights issues,
  bonus shares, stock dividends), entered by the owner from official IDX/KSEI announcements.
- **Precedence:** `manual_csv` > `eodhd`, per *event family* and ex-date (share-count, cash
  dividend, rights). EODHD reports IDX bonus shares as "splits", so a manual `bonus_shares` and
  a vendor `stock_split` on the same date are one event and are never applied twice.

### Storage
- `corporate_actions` is versioned like prices (ADR 0003): one current row per
  (source, instrument, type, ex_date). A revision is flagged by default, or stored as a new
  version with `--supersede`.
- `price_adjustment_factors`: **one factor per action**, versioned. The `inputs` column records
  everything the factor was computed from (ratio, dividend, reference date and close, price
  source, action version). Changed inputs or method → new `factor_version`; the old one is kept.
  A factor whose action is no longer chosen is retired, not deleted.
- `artifact_fetches` logs **every retrieval**. The raw archive stores identical bodies once
  (content-addressed), so without this log "was TLKM's split list checked?" could not be
  answered when TLKM's empty list was byte-identical to another ticker's (bug found in the live
  run).
- `reconciliation_results` keeps every check outcome.

### Formulas (prices before the ex-date are multiplied by f)
| Event | Factor | Series |
|---|---|---|
| split / reverse split / bonus / stock dividend | `old / new` | split + total-return |
| cash dividend D | `1 − D / C`, C = raw close of the **last session before** the ex-date | total-return |
| rights issue (new per old at S) | `min(1, TERP / C)`, `TERP = (C·old + S·new)/(old+new)` | total-return |

- **Volume** is divided by share-count factors only.
- **Missing inputs:** if C is missing or D ≥ C, no factor is stored. An `adjustment_unresolved`
  error issue is opened once and any earlier factor for that action is retired. Downstream stages
  must hold predictions for that instrument (PRD §8.3).
- **Unsupported types:** mergers, private placements and the like are stored with an INFO note
  and get no factor.

### Series (computed on read, `idx prices show`)
- `adjusted_bars(instrument, raw | split | total_return, as_of=…)`. The cumulative factor for a
  bar on date t is the product of factors with `t < ex_date ≤ as_of`, so a backtest on day A
  sees exactly what could have been adjusted on day A.
- Prices come from the best available source per date (`manual_csv` > `eodhd`).
- **EODHD volume** (split-adjusted at retrieval, OI-17) is converted back to raw using splits in
  `(t, retrieval date]`. `volume_verified=False` when no EODHD split list for the ticker was
  fetched on or after that retrieval date.

### Reconciliation (`idx ca reconcile`)
- **Vendor check:** our cumulative total-return factor, as of the retrieval date, must reproduce
  EODHD's `adjusted_close / close` within 0.2% (pass), 1% (warn), or else fail.
- **Split vs price:** on a share-count ex-date, `close_ex / close_prev` must be within
  0.75–1.33× of the factor. Outside that band → fail plus a `corporate_action_inconsistency`
  issue.

## Verification (live, owner's free EODHD key, 10 API calls)
- BBCA, BBRI, TLKM, BMRI, ASII, past year: 12 cash dividends, no splits, all valid.
- Implied yields 0.3–8.1% (plausible for these issuers). BBCA's raw close fell 6,700 → 6,450 on
  the Rp 281 ex-date.
- **Vendor reconciliation: 5/5 pass, maximum difference 0.000002%** (EODHD rounds to 4
  decimals). This shows our method and dividend set reproduce EODHD's adjustment exactly. It
  does **not** independently verify the dividend amounts against IDX announcements; both come
  from EODHD.
- Recomputing the factors changes nothing. Dropping the database and replaying the archive
  restores identical prices, actions, factors and reconciliation results with zero API calls.

## Consequences / limits
- Free plan: only the past year of corporate actions. Older history needs manual entry, or it
  stays unadjusted and is flagged (OI-26).
- EODHD supplies no declaration, record or payment dates for JK (all null). Announcement-time
  point-in-time knowledge is therefore not available (OI-25); ex-date point-in-time is.
