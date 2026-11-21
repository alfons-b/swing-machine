"""Full (PSAK, bilingual) statements on synthetic text that mimics real extraction quirks."""

from datetime import date
from decimal import Decimal as Dec

from idx_trading.fundamentals.full_statements import (
    check_full_identities,
    eps_share_check,
    heal_numbers,
    parse_document,
    parse_full_statements,
)

M = 1_000_000

BS = """PT CONTOH TBK
LAPORAN POSISI KEUANGAN
KONSOLIDASIAN INTERIM
30 JUNI 2025 DAN 31 DESEMBER 2024
(Disajikan dalam jutaan Rupiah, kecuali dinyatakan lain)
     Catatan/    30 Juni/   31 Desember/
     Notes   June 2025   December 2024
ASET         ASSETS
ASET LANCAR        CURRENT ASSETS
Kas dan setara kas 4  10,513,725  4,751,621  Cash and cash equivalents
Piutang usaha, bersih 5      Trade receivables, net
-  Pihak ketiga   1,449,038  943,915  Third parties -
Jumlah aset lancar   20,959,065  17,991,975  Total current assets
Kas dan setara kas Restricted cash
 yang dibatasi penggunaannya  9  368,043  360,988   and cash equivalents
JUMLAH ASET   48,378,149  44,522, 645  TOTAL ASSETS

Jumlah liabilitas jangka pendek   12,089,153  9,770 ,898 Total current liabilities
JUMLAH LIABILITAS   14,671,650  12,323,139 TOTAL LIABILITIES
EKUITAS      EQUITY
 seri B; Modal ditempatkan dan      shares; Issued and fully paid capital
 disetor penuh - 1 saham preferen      - 1 preferred Series A Dwiwarna
 24.030.764.724 saham biasa      series B ordinary shares
 Rp100 (nilai penuh) per saham 23  2,403,076  2,403 ,076 (full amount) per share
- Belum ditentukan penggunaannya   15,553,883  14,50 5,151 Unappropriated  -

Jumlah ekuitas yang dapat
 diatribusikan kepada pemilik      Total equity attributable to
 entitas induk   32,518,005  31,458,113 owners of the parent
Kepentingan nonpengendali 40  1,188,494  741,393 Non-controlling interests
JUMLAH EKUITAS   33,706,499  32,199,506  TOTAL EQUITY
JUMLAH LIABILITAS DAN EKUITAS   48,378,149  44,522,645 TOTAL LIABILITIES AND EQUITY
"""

PL = """PT CONTOH TBK
LAPORAN LABA RUGI DAN PENGHASILAN
KOMPREHENSIF LAIN KONSOLIDASIAN INTERIM
UNTUK PERIODE-PERIODE ENAM BULAN YANG
BERAKHIR 30 JUNI 2025 DAN 2024
(Disajikan dalam jutaan Rupiah, kecuali dinyatakan lain)
PENJUALAN 26        59,019,725  23,189,493 SALES
LABA KOTOR   8,237,665  2,003,483  GROSS PROFIT
LABA USAHA    6,137,603  532,327  OPERATING PROFIT
LABA SEBELUM PAJAK PENGHASILAN  6,533,024  1,650,17 0 PROFIT BEFORE INCOME TAX
Beban pajak penghasilan 18c   (1,389,553)  (137,242) Income tax expense
LABA PERIODE BERJALAN   5,143,471  1,512,928 PROFIT FOR THE PERIOD

LABA PERIODE
 BERJALAN YANG DAPAT        PROFIT FOR THE PERIOD
 DIATRIBUSIKAN KEPADA:       ATTRIBUTABLE TO:
 Pemilik entitas induk    4,696,370  1,550,531 Owners of the parent
 Kepentingan nonpengendali   447,101  (37,603) Non-controlling interests

JUMLAH PENGHASILAN
 KOMPREHENSIF PERIODE      TOTAL COMPREHENSIVE INCOME
 BERJALAN YANG DAPAT       FOR THE PERIOD
 DIATRIBUSIKAN KEPADA:      ATTRIBUTABLE TO:
 Pemilik entitas induk   4,707,102  1,641,551 Owners of the parent
 Kepentingan nonpengendali   447,101  (37,603) Non-controlling interests

LABA BERSIH PER SAHAM
 DASAR DAN DILUSIAN       BASIC AND DILUTED EARNINGS
 KEPADA PEMILIK ENTITAS TO OWNERS OF THE PARENT
 INDUK (NILAI PENUH) 33  195.43  64.52  (FULL AMOUNT)
"""

NOTES = """CATATAN ATAS LAPORAN KEUANGAN
Laporan posisi keuangan konsolidasian disusun ... Jumlah liabilitas 1,000 2,000 Total liabilities
"""


def parse(bs=BS, pl=PL):
    return parse_full_statements(["cover", bs, pl, NOTES])


def test_heal_numbers():
    assert heal_numbers("12,089,153  9,770 ,898 X") == "12,089,153  9,770,898 X"
    assert heal_numbers("1,349,587  1,338,42 7 currency") == "1,349,587  1,338,427 currency"
    assert heal_numbers("15,553,883  14,50 5,151 U") == "15,553,883  14,505,151 U"
    assert heal_numbers("48,378,149  44,522, 645 T") == "48,378,149  44,522,645 T"
    assert heal_numbers("1,650,17 0 P") == "1,650,170 P"
    assert heal_numbers("10  20  30") == "10  20  30"  # separate numbers stay separate


def test_parse_full_statement_values_dates_and_signs():
    r = parse()
    assert r.errors == [] and check_full_identities(r) == []
    assert r.period_end == date(2025, 6, 30) and r.currency == "IDR"
    g = lambda c, d: r.get(c, "consolidated", d)  # noqa: E731
    assert g("cash", date(2025, 6, 30)) == Dec(10_513_725 * M)  # not the restricted-cash row
    assert g("current_liabilities", date(2024, 12, 31)) == Dec(9_770_898 * M)
    assert g("equity_parent", date(2025, 6, 30)) == Dec(32_518_005 * M)
    assert g("revenue", date(2024, 6, 30)) == Dec(23_189_493 * M)
    assert g("profit_before_tax", date(2024, 6, 30)) == Dec(1_650_170 * M)  # healed number
    assert g("income_tax", date(2025, 6, 30)) == Dec(1_389_553 * M)  # stored as expense (+)
    assert g("net_income_nci", date(2024, 6, 30)) == Dec(-37_603 * M)
    assert g("net_income_parent", date(2025, 6, 30)) == Dec(4_696_370 * M)  # not comprehensive
    assert g("eps_basic", date(2025, 6, 30)) == Dec("195.43")
    assert g("shares_issued", date(2025, 6, 30)) == 24_030_764_724
    pl = next(f for f in r.facts if f.concept == "revenue" and f.period_end == date(2025, 6, 30))
    assert pl.period_start == date(2025, 1, 1)  # six months ended 30 June
    assert eps_share_check(r) == []  # 4,696,370m / 24.03bn = 195.43


def test_period_length_from_title():
    r = parse(
        pl=PL.replace("ENAM BULAN", "SEMBILAN BULAN").replace("30 JUNI", "30 SEPTEMBER"),
        bs=BS.replace("30 JUNI 2025", "30 SEPTEMBER 2025"),
    )
    f = next(f for f in r.facts if f.concept == "revenue" and f.period_end == date(2025, 9, 30))
    assert f.period_start == date(2025, 1, 1)
    annual = PL.replace("UNTUK PERIODE-PERIODE ENAM BULAN YANG", "UNTUK TAHUN YANG").replace(
        "BERAKHIR 30 JUNI 2025 DAN 2024", "BERAKHIR 31 DESEMBER 2025 DAN 2024"
    )
    r = parse(
        bs=BS.replace("30 JUNI 2025 DAN 31 DESEMBER 2024", "31 DESEMBER 2025 DAN 31 DESEMBER 2024"), pl=annual
    )
    f = next(f for f in r.facts if f.concept == "net_income" and f.period_end == date(2024, 12, 31))
    assert f.period_start == date(2024, 1, 1)


def test_usd_and_thousands_units():
    r = parse(
        bs=BS.replace("jutaan Rupiah", "ribuan Dolar Amerika Serikat"),
        pl=PL.replace("jutaan Rupiah", "ribuan Dolar Amerika Serikat"),
    )
    assert r.currency == "USD"
    assert r.get("total_assets", "consolidated", date(2025, 6, 30)) == Dec(48_378_149 * 1000)
    assert r.get("eps_basic", "consolidated", date(2025, 6, 30)) == Dec("195.43")  # full amount


def test_identity_failure_and_missing_title():
    broken = BS.replace("JUMLAH EKUITAS   33,706,499", "JUMLAH EKUITAS   33,706,999")
    r = parse(bs=broken)
    assert any("liabilities" in p for p in check_full_identities(r))
    r = parse_full_statements(["no statements here"])
    assert r.errors


def test_eps_share_mismatch_is_a_warning():
    r = parse(bs=BS.replace("24.030.764.724 saham biasa", "20.000.000.000 saham biasa"))
    assert check_full_identities(r) == [] and eps_share_check(r)


def test_dispatch_picks_the_right_parser():
    from tests.fakes import bank_publication_pages

    parsed, problems, _ = parse_document(["cover", BS, PL])
    assert parsed.document_type == "financial_statements" and problems == []
    bank = bank_publication_pages(
        end=date(2026, 3, 31),
        prev_bs=date(2025, 12, 31),
        prev_pl=date(2025, 3, 31),
        ta=1_000_000,
        tl=850_000,
        te=150_000,
        ni=10_000,
        nip=9_990,
        nci=10,
        pbt=12_500,
        eps=119,
        eps_prev=115,
    )
    parsed, problems, _ = parse_document(bank)
    assert parsed.document_type == "bank_publication_quarterly" and problems == []
