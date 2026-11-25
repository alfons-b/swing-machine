# Financial statements to download

Automated where an issuer's site allows it (see docs/agent-week-plan.md, M1.7); otherwise
the owner downloads from idx.co.id and attaches the PDF in the chat.

Latest interim statements (30 Sep 2026) first, then FY2025 (31 Dec 2025) for TTM.
Order follows `config/priority_tickers.txt`. Tick an item once it is imported.
Record the publication date (IDX announcement date or the issuer's own date). If it is not
shown, use the earliest press report of the figures and pass `--date-from-press`.

| Ticker | Period | Status |
|---|---|---|
| BBRI | 2026-09-30 | done 2026-11-25 (full statements; published 2026-11-09, press) |
| BMRI | 2026-09-30 | todo |
| BBNI | 2026-09-30 | done 2026-11-25 (published 2026-10-27, press) |
| TLKM | 2026-09-30 | done 2026-11-25 (published 2026-11-02, press) |
| ASII | 2026-09-30 | done 2026-11-25 (published 2026-10-29, press) |
| AMMN | 2026-09-30 | rejected: non-tax government revenue after tax breaks PBT − tax = NI (OI-41); USD |
| UNVR | 2026-09-30 | done 2026-11-25 (published 2026-10-23, press) |
| ICBP | 2026-09-30 | done 2026-11-25 (no dated press report: date = owner upload, conservative) |
| INDF | 2026-09-30 | done 2026-11-25 (published 2026-10-31, press) |
| ANTM | 2026-09-30 | done 2026-11-25 (published 2026-10-28, press) |
| ANTM | 2025-12-31 | done 2026-11-25 (published 2026-03-31, press) |
| BBCA | — | done automatically (bca_ir) |

## Next: FY2025 (31 Dec 2025) for TTM

PER, ROE and ROA need FY2025 alongside the 30 Sep 2026 report (TTM = FY2025 + 9M2026 − 9M2025).
Only ANTM has it so far.

| Ticker | Period | Status |
|---|---|---|
| BBRI | 2025-12-31 | todo |
| BBNI | 2025-12-31 | todo |
| BMRI | 2025-12-31 | todo |
| TLKM | 2025-12-31 | todo |
| ASII | 2025-12-31 | todo |
| UNVR | 2025-12-31 | todo |
| ICBP | 2025-12-31 | todo |
| INDF | 2025-12-31 | todo |
