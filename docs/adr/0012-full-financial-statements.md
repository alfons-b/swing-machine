# ADR 0012 — Full financial statements (non-bank issuers) and the data-expansion run

- Status: Accepted
- Date: 2026-11-21
- Extends: ADR 0010. Closes, in part: OI-29 (non-bank parser), OI-30 (owner decision), OI-31

## Owner decisions (2026-11-21)
1. BCA IR automation: **yes**, with the honest client identity
   `idx-trading/<version> (personal research)` (no library token, no browser imitation).
   The same identity is now the default for every source.
2. Non-bank statements: the owner supplies the documents. First sample: **ANTAM (ANTM),
   interim consolidated statements for 30 June 2025** (145 pages, bilingual, PSAK).
3. Merge to `main` directly.
4. Expand the data as far as the Rp0 budget allows.

## Parser for full statements (`fundamentals/full_statements.py`)
- **Statement pages:** the title ("LAPORAN POSISI KEUANGAN", "LAPORAN LABA RUGI") must be a
  line near the top of the page *and* the units line "(Disajikan dalam …)" must be present.
  Notes pages mention the same words in running text and are excluded (bug found on ANTAM).
- **Header information from the titles:** dates ("30 JUNI 2025 DAN 31 DESEMBER 2024";
  "BERAKHIR 30 JUNI 2025 DAN 2024"), period length (TIGA/ENAM/SEMBILAN BULAN, or TAHUN → 12),
  units (jutaan/ribuan × Rupiah/Dolar AS). EPS is always "nilai penuh" (full amount).
- **Rows:** `<Indonesian label> [note] <current> <prior> <English label>`, English number
  format.
  - Split numbers from text extraction (`9,770 ,898`, `1,338,42 7`, `14,50 5,151`,
    `44,522, 645`) are re-joined only when the result forms valid 3-digit groups.
  - Labels that wrap over lines are matched from the shortest candidate (the line alone)
    widening one line at a time, so section headings above a row do not break anchored
    patterns (bug found on ANTAM's "Kas dan setara kas").
- **Attribution:** owners/NCI rows are read only inside the "laba … diatribusikan" block, not
  the comprehensive-income block that repeats them.
- **Tax sign:** the identity PBT − tax = NI decides how the printed tax is read, then it is
  stored as an expense (+). It is never assumed.
- **Shares:** issued ordinary shares are read from the share-capital caption
  ("ditempatkan … 24.030.764.724 saham biasa"). Reported EPS is checked against owners'
  profit ÷ shares (> 1% apart → `fin_eps_share_mismatch` warning, for example because of
  treasury shares).
- **Identities required:** assets = liabilities + equity; parent + NCI equity = total equity;
  PBT − tax = NI; owners + NCI = NI.
- **Currency** is stored per report. Ratios refuse price-based values for non-IDR statements
  (no FX conversion yet).
- `parse_document` picks the bank-publication or full-statement parser from the content.
  Identical bytes arriving through another channel are recognised as the same document (bug
  found when BCA files came both manually and automatically).

## ANTAM result (live)
- 38 facts across 19 concepts.
- All identities hold. Split numbers repaired (`9,770,898`, `1,650,170`).
- Shares 24,030,764,724; EPS 195.43 = Rp 4,696,370 million ÷ shares.

## Ratios now prefer reported inputs
Issued shares (exact) are used before EPS-implied shares, and parent equity before total
equity. Notes say which was used.

## Data-expansion run (Rp0)
- **Master:** `idx instruments import-eodhd` added 918 EODHD active common stocks (923 total).
  No listing dates are invented: their status is `unknown`, so they are collected but not
  BUY-eligible until the IDX company list is imported (OI-23).
- **Prices:** the EODHD free key's one-off 500 bonus calls fetch one year of daily bars for
  about 420 stocks, in the order set by `config/priority_tickers.txt` (an agent-chosen list of
  large and liquid names, **not** an official index; edit freely), then the remaining ones
  daily within the free limit.
- **Delivery to the owner:** the data lives in a backup package (`pg_dump` + manifest + raw
  archive). It is restored with `idx backup restore --dir …` and the raw files are copied to
  `data/raw`. Vendor data is never committed to the public repository (redistribution is
  denied in the registry).
