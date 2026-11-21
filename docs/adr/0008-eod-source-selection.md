# ADR 0008 — Automated EOD source: EODHD

- Status: Accepted (decision delegated to the agent by the system owner)
- Date: 2026-11-21
- Closes, in part: OI-01, OI-04

## Context
M1.2 needs one automated EOD source for IDX stocks. It must follow PRD §6.2: the use must be
permitted, with no bypassing of authentication, paywalls, rate limits or bot protection. It must
also fit the Rp0 budget (§18.4). Every candidate was checked on 2026-11-21, from the live
terms and pricing pages where they could be reached.

## Options evaluated

| Source | IDX coverage | Free? | Terms on automated use and storage | Verdict |
|---|---|---|---|---|
| **IDX website** (idx.co.id) | Full, official | — | Every automated request, including `robots.txt`, gets a Cloudflare "Attention Required" challenge (HTTP 403). IDX sells EOD/historical data through *IDX Data Services* (priced catalogue). | **Rejected.** Passing the challenge = bypassing an access control. Manual downloads by the owner are still possible via `manual_csv` |
| **Yahoo Finance** / yfinance | Yes (.JK) | Yes | ToS forbid collecting data "using any automated means … for any purpose without our express, prior permission", and using data "to create any database, archive". | **Rejected** (`blocked`) |
| **Stooq** | Not verified | — | Site reset the connection; Indonesian coverage not confirmed. | Not usable |
| **Twelve Data** | Yes (XIDX) | Basic: 800 calls/day | Allows storage for "Internal Use", but the IDX exchange page is marked **Pro+** (from USD 99/mo). | **Rejected for Rp0**; registered as a paid alternative |
| **Invezgo** (ID) | Yes, rich IDX-specific data | No, from IDR 499,900/mo | Not reviewed | Post-MVP enrichment candidate |
| **Sectors.app** (ID) | Yes | Unknown | Pages return HTTP 429 to scripted requests; not reviewed | Open |
| Alpha Vantage, Marketstack, iTick, Kainex | Unconfirmed / marketing claims only | — | Not verified | Not chosen |
| **EODHD** | Yes, exchange `JK` (MIC XIDX); active **and delisted** ticker lists | Free: 20 calls/day + 500 one-off, **past year only**; paid from USD 19.99/mo | Non-Professional Users "are permitted to store, manipulate, and analyze the data for private, non-commercial purposes". Sharing the account and redistribution are prohibited. | **Chosen** |

## Decision
1. **EODHD** is the automated EOD source (`source_id: eodhd`). It is approved for
   `automated_ingestion` and `raw_archive`, and denied for `redistribution`. Only the owner may
   use the data (PRD §4.1), and never commercially.
2. The adapter is built for both plan levels; no code change is needed to move between them:
   - **Free plan (Rp0, default):** per-ticker `/eod` calls at 1 call per ticker for any date
     window. `idx ingest eodhd` plans the run under a hard call budget
     (`IDX_EODHD_DAILY_CALL_BUDGET`, default 20). Tickers never fetched come first, then the
     stalest, and each chosen ticker is brought fully up to date in one call.
   - **Paid plan:** `idx ingest eodhd-bulk --date D` gets the whole exchange for one session for
     a flat 100 calls, with 30+ years of history per ticker. Bulk is refused while the budget is
     below 100.
3. **Full-market coverage at Rp0 still relies on manual files.** 20 calls a day keeps about 20
   tickers current; IDX has about 950. The PRD's "all common stocks daily" goal at Rp0 is met by
   importing the IDX stock summary by hand (`manual_csv`). EODHD free acts as the automated
   feed for a watchlist and as a second source for cross-checking (M1.5). Moving to the paid
   plan (about USD 20/month) is the owner's budget decision; this ADR does not make it.
4. **Data semantics stay explicit.** EODHD OHLC is raw, as traded, and goes into `daily_prices`.
   `adjusted_close` (split- and dividend-adjusted) stays only in the raw archive. `volume` is
   **split-adjusted**; every payload is tagged `volume_basis=split_adjusted` (OI-17). EODHD has
   no traded value, trade frequency or foreign flow (OI-18).
5. **Instrument master:** `idx instruments eodhd-diff` only *compares* EODHD's ticker lists with
   the master. It writes nothing, because EODHD gives no listing or delisting dates, and
   guessing them would corrupt ticker history (ADR 0002).
6. **Safety in the client:** it identifies itself honestly (no browser user-agent), uses https
   only, and throttles below the 1,000 requests/minute limit. It retries only 429/5xx and network
   errors, honouring `Retry-After`. 401/402/403 are fatal and never retried. The API token comes
   only from `IDX_EODHD_API_TOKEN` and is redacted from every URL that is stored or logged.

## Verification
- Unit and integration tests use a fake EODHD server built on the documented response formats
  (`tests/fakes.py`).
- A live check against the real API with EODHD's public `demo` key (AAPL.US, 2026-11-16…20)
  parsed the real response without errors, stored the token as `***`, and treated a JK request
  under the demo key (HTTP 403) as fatal without retrying.
- **Live JK test with the owner's free key (2026-11-21):** 7 API calls (2 symbol lists and 5
  tickers × 1 year) → 1,195 bars, 0 rejected, 0 warnings. All 239 calendar sessions are present;
  all OHLC values lie on the IDX tick grid (so they are raw, not adjusted); EODHD's usage counter
  matched the system's own count. Rebuilding the database from the raw archive alone
  (`idx ingest replay`) reproduced identical rows with no API calls.
- **Coverage found:** 923 active JK common stocks (plus 1 fund) and only 97 delisted entries,
  26 of which are warrants labelled "Common Stock" (OI-02, OI-21).

## Consequences
- Turning on another automated source later means a new adapter plus a reviewed change to
  `sources.yaml`.
- Review again if EODHD's terms or free plan change (check the terms URL at least every 6 months).
