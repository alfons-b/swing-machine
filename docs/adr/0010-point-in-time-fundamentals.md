# ADR 0010 — Point-in-time fundamentals: sources and design

- Status: Accepted (sources partly on hold, see below)
- Date: 2026-11-21
- Implements: FR-006 (partial), FR-007, FR-018 (fundamentals part), PRD §7.3
- Owner decision (2026-11-21): budget Rp0; "the agent sources the financial statements itself"

## Source review (2026-11-21)

| Source | Finding | Decision |
|---|---|---|
| **IDX** `www.idx.co.id` | Every automated request gets a Cloudflare challenge (ADR 0008). | Denied |
| **IDX** `www.idx.id` (new front end) | Reachable, no robots.txt. But its script bundles load all data and files from `www.idx.co.id` (88 references; financial reports via `/primary/ListedCompany/GetFinancialReport`). | **Not used.** Calling the same protected API through another hostname would sidestep IDX's bot protection |
| **EODHD fundamentals** | Not in the free plan (pricing table). | Rp0 budget: not used |
| **Issuer IR sites**, BCA first | BCA lists its OJK-mandated quarterly publication reports in a structured list on its IR page. The file name starts with the publication date. The site has product terms only, no website or content terms; robots.txt allows the report paths. | Reviewed as acceptable for private analysis, but **automated download is on hold**: the site's firewall answers a bare `403 Forbidden` to clients identifying as `python-urllib`, while honest custom identities got 200. Changing the client identity *only* to pass that filter would be evading an access control, so this is left to the owner (`bca_ir.automated_ingestion = pending_review`) |
| Manual import | `idx fin import-pdf` takes a PDF the owner (or a reviewed process) obtained, with ticker, publication date and origin stated. | Available now (`manual_csv`) |

**Coverage reality at Rp0.** No legal, free, automated source covers financial statements for
all ~900 IDX issuers. The OJK **bank publication report** has a prescribed layout that is the
same for every Indonesian bank, so one parser covers all listed banks; each bank's site needs
its own terms review before automated use. Non-bank issuers publish free-form statements
(BCA's full statements are 183 pages) that cannot be extracted reliably without a structured
source. That stays open (OI-29).

## Design

### Parsing (`fundamentals/bank_publication.py`)
- Works on extracted page **text** (`pypdf`, BSD licence), so it is tested on synthetic text and
  no issuer PDF is ever committed (that would be redistribution).
- Column dates and audit status are read from each statement's own header line, not assumed.
  Columns: individual current/prior, consolidated current/prior; amounts × 1,000,000
  ("dalam jutaan Rupiah"). EPS is in whole rupiah, consolidated only.
- Only explicitly mapped labels are used (16 canonical concepts, `concepts.py`). A mapped label
  seen twice, a row with the wrong number of columns, or a missing header is an **error**,
  never a guess. "Owners"/"NCI" lines are read only inside the net-profit attribution block,
  not the comprehensive-income block that repeats the same labels.
- **Identities must hold** (±Rp 2 million rounding) before anything is stored:
  assets = liabilities + equity; profit before tax − tax = net profit;
  owners + NCI = net profit — for every basis and period.

### Point in time (`fundamentals/pit.py`)
- `published_at` = publication date 00:00 WIB. Its basis is recorded:
  `filename_date` (the issuer's own date prefix) or `stated_by_owner`.
- `available_at` = **the next day 00:00 WIB**. An EOD signal for day D never sees a report
  published on D (the time of day is unknown).
- A re-published document is a new `revision_id`; nothing is overwritten.
  `fact_as_of(…, as_of)` uses the most recently available report that carries the period, so
  restatements appear only from the day they were published.
- The comparison columns of every new report are checked against earlier reports. A difference
  raises a `fin_comparative_restated` warning.
- TTM = FY(y−1) + YTD(y) − YTD(y−1) for duration concepts; it is undefined if any part is
  unknown on `as_of`.

### Ratios (`fundamentals/ratios.py`, FR-007)
- EPS TTM, PER, implied shares (± stated uncertainty, because EPS is printed rounded), BVPS,
  PBV, ROE, ROA, DER, LDR and YoY profit growth.
- Prices are raw closes from `manual_csv` > `eodhd`. EPS and shares are restated for
  share-count events after the period end.
- Missing inputs → the ratio is `None` with a note, never estimated.
- Ratios are computed on read. They will be materialised in the Phase 2 feature store with
  their `as_of`.

## Verification (live, BCA, two real reports)
- Reports used: FY2025 (published 2026-01-27, audited) and Q1 2026 (published 2026-04-23,
  unaudited). The files were downloaded during the terms review with an honest client identity
  and imported via the manual path with their original URL and filename date.
- 60 facts per report; all identities hold. The Q1 report's Dec-2025 comparison values equal
  the FY report exactly (no restatement).
- Q1 2026 consolidated net profit attributable to owners = Rp 14,684,123 million, matching
  press coverage (≈ Rp 14.7 trillion). YoY growth computed: 3.80% (press: 3.83%).
- Point in time: as of 2026-04-23 the ratios still use FY2025; from 2026-04-24, Q1 2026.
- EPS TTM 471 = 467 + 119 − 115. Implied shares ≈ 123.2–123.4 billion, consistent between the
  two reports within the stated EPS-rounding uncertainty.
