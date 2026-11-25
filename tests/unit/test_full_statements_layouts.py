"""Layout variants met on real 2025/2026 IDX statements (ADR 0012 addendum), on synthetic text:
Indonesian number format, notes between the value columns, column-header dates, vertical
layout, untitled continuation pages, mistitled pages, parent-only appendices, tax without a
total, discontinued operations, encrypted PDFs."""

import io
from datetime import date
from decimal import Decimal as Dec

from idx_trading.fundamentals.full_statements import (
    _split_row,
    check_full_identities,
    number_format,
    parse_document,
    parse_full_statements,
)

M = 1_000_000
B = 1_000_000_000

DISCLAIMER = (
    "Catatan atas laporan keuangan konsolidasian interim terlampir\nmerupakan bagian yang tidak terpisahkan\n"
)

# BBRI-like: Indonesian numbers, single title date, dates in the column headers, notes between
# the columns, wrapped "LABA SEBELUM / BEBAN PAJAK" label, attribution under a comprehensive block.
BANK_BS = (
    DISCLAIMER
    + """PT BANK CONTOH Tbk
LAPORAN POSISI KEUANGAN
KONSOLIDASIAN INTERIM
Tanggal 30 September 2026
(Disajikan dalam jutaan Rupiah,
kecuali dinyatakan lain)
30 September 2026/  Catatan/  31 Desember 2025/
September 30, 2026 Notes December 31, 2025
Kas 25.694.774 3 32.044.482 Cash
Giro pada Bank Lain 42.306.668   5,44 42.444.581 Current accounts with Other Banks
TOTAL ASET 2.337.595.443 2.135.371.105 TOTAL ASSETS
TOTAL LIABILITAS 1.991.910.718 1.804.429.671 TOTAL LIABILITIES
Total ekuitas yang dapat diatribusikan Total equity attributable to equity
 kepada entitas induk 338.859.579 324.033.858 holders of the parent entity
Kepentingan non-pengendali 6.825.146   31h 6.907.576 Non-controlling interest
TOTAL EKUITAS 345.684.725 330.941.434 TOTAL EQUITY
TOTAL LIABILITAS DAN EKUITAS 2.337.595.443 2.135.371.105 TOTAL LIABILITIES AND EQUITY
"""
)
BANK_PL = (
    DISCLAIMER
    + """PT BANK CONTOH Tbk
LAPORAN LABA RUGI
DAN PENGHASILAN KOMPREHENSIF LAIN
Untuk Periode Sembilan Bulan yang Berakhir
pada Tanggal 30 September 2026
(Disajikan dalam jutaan Rupiah,
kecuali dinyatakan lain)
2025
2026 Notes Unreviewed)
Pendapatan jasa asuransi 5.311.301   2ac 6.854.545 Insurance service income
LABA SEBELUM INCOME BEFORE
 BEBAN PAJAK 61.008.262 52.863.561 TAX EXPENSE
BEBAN PAJAK (12.849.917) 38b (11.631.325) TAX EXPENSE
LABA BERSIH 48.158.345 41.232.236 NET INCOME
TOTAL PENGHASILAN TOTAL COMPREHENSIVE
LABA PERIODE BERJALAN
 YANG DAPAT DIATRIBUSIKAN INCOME FOR THE PERIOD
 Pemilik entitas induk 47.688.997 40.778.810 Equity holders of the parent entity
 Kepentingan non-pengendali 469.348 453.426 Non-controlling interest
TOTAL 48.158.345 41.232.236 TOTAL
TOTAL PENGHASILAN TOTAL COMPREHENSIVE
PERIODE BERJALAN YANG DAPAT FOR THE PERIOD
 DIATRIBUSIKAN KEPADA: ATTRIBUTABLE TO:
 Pemilik entitas induk 45.627.531 44.933.265 Equity holders of the parent entity
 Kepentingan non-pengendali 175.744 707.933 Non-controlling interest
TOTAL 45.803.275 45.641.198 TOTAL
LABA PERIODE BERJALAN EARNINGS PER SHARE
 PER SAHAM DASAR YANG ATTRIBUTABLE TO EQUITY
 Dasar 317 271 Basic
"""
)
# parent-only appendix later in the document: same titles, other numbers
PARENT_ONLY = BANK_BS.replace("2.337.595.443", "9.999.999.999")


def test_number_format_detection():
    assert number_format(["Kas 25.694.774 3 32.044.482"]) == "id"
    assert number_format(["Kas 10,513,725 4,751,621"]) == "en"
    # statements in billions rarely reach two groups (TLKM): one group still decides
    assert number_format(["Kas dan setara kas 3,32,37 33.626 34.228", "Laba 19.462 19.842"]) == "id"
    assert number_format(["EPS 195.43 64.52", "Kas 1,234 5,678"]) == "en"


def test_notes_between_value_columns_are_skipped():
    from idx_trading.fundamentals.full_statements import _NUM_EN, _NUM_ID

    assert _split_row("Kas 25.694.774 3 32.044.482 Cash", _NUM_ID) == (
        "Kas Cash",
        ["25.694.774", "32.044.482"],
    )
    assert _split_row("Giro 42.306.668   5,44 42.444.581 Current", _NUM_ID)[1] == ["42.306.668", "42.444.581"]
    assert _split_row("Pendapatan 5.311.301 2ac 6.854.545 Income", _NUM_ID)[1] == ["5.311.301", "6.854.545"]
    assert _split_row("Beban pajak (851,406) 2q, 14a (910,259) Tax", _NUM_EN)[1] == ["(851,406)", "(910,259)"]
    assert _split_row("Pajak tangguhan 2z.iii 130 325", _NUM_ID)[1] == ["130", "325"]
    # a leading note stays out of the values (old layout)
    assert _split_row("Kas dan setara kas 4 10,513,725 4,751,621 Cash", _NUM_EN)[1] == [
        "10,513,725",
        "4,751,621",
    ]


def test_bank_layout_dates_notes_wrapped_labels_and_parent_only_appendix():
    p = parse_full_statements(["cover", BANK_BS, BANK_PL, "notes", PARENT_ONLY, BANK_PL])
    assert p.errors == []
    assert check_full_identities(p) == []
    assert p.period_end == date(2026, 9, 30)
    g = lambda c, d=date(2026, 9, 30): p.get(c, "consolidated", d)  # noqa: E731
    assert g("total_assets") == Dec(2_337_595_443) * M  # not the parent-only 9.999...
    assert g("total_assets", date(2025, 12, 31)) == Dec(2_135_371_105) * M
    assert g("profit_before_tax") == Dec(61_008_262) * M
    assert g("income_tax") == Dec(12_849_917) * M
    assert g("net_income_parent") == Dec(47_688_997) * M  # not the comprehensive-income line
    assert g("equity_parent") == Dec(338_859_579) * M
    assert g("eps_basic") == Dec(317)
    assert p.get("net_income", "consolidated", date(2025, 9, 30)) == Dec(41_232_236) * M
    ni = next(f for f in p.facts if f.concept == "net_income" and f.period_end == date(2026, 9, 30))
    assert ni.period_start == date(2026, 1, 1)


# INDF-like: vertical layout, blank lines, untitled continuation page, English after numbers
V_BS1 = (
    DISCLAIMER
    + """PT CONTOH VERTIKAL TBK
LAPORAN POSISI KEUANGAN
KONSOLIDASIAN INTERIM
Tanggal 30 September 2026
(Disajikan dalam jutaan Rupiah,
Kecuali Dinyatakan Lain)
Catatan/
Notes
30 September 2026/
September 30, 2026
31 Desember 2025/
December 31, 2025
TOTAL ASET  36  231.365.786  217.981.765  TOTAL ASSETS
"""
)
V_BS2 = (
    DISCLAIMER
    + """2
 Catatan/
Notes
30 September 2026/
31 Desember 2025/
TOTAL LIABILITAS

36

102.240.852

97.746.518
TOTAL LIABILITIES
Ekuitas yang Dapat Diatribusikan
kepada Pemilik Entitas Induk
79.860.441  73.147.337
Equity Attributable to Equity
Holders of the Parent Entity
Kepentingan Nonpengendali  24  49.264.493   47.087.910   Non-controlling Interests
TOTAL EKUITAS  129.124.934  120.235.247  TOTAL EQUITY
"""
)
V_PL = (
    DISCLAIMER
    + """PT CONTOH VERTIKAL TBK
LAPORAN LABA RUGI DAN PENGHASILAN
KOMPREHENSIF LAIN KONSOLIDASIAN INTERIM
Untuk Periode Sembilan Bulan yang Berakhir pada Tanggal 30 September 2026 dan 2025
(Disajikan dalam jutaan Rupiah,
Kecuali Dinyatakan Lain)
LABA SEBELUM BEBAN PAJAK
PENGHASILAN  18,36

15.263.686  14.698.342
INCOME BEFORE INCOME TAX
EXPENSE
Beban pajak penghasilan  18,36  (3.390.262  ) (3.330.063 ) Income tax expense
LABA PERIODE BERJALAN  36  11.873.424  11.368.279  INCOME FOR THE PERIOD
Laba periode berjalan yang dapat
diatribusikan kepada:
Pemilik entitas induk  25  7.865.768   7.881.347  Equity holders of the parent entity
Kepentingan nonpengendali    4.007.656   3.486.932  Non-controlling interests
"""
)
NOTES_PAGE = "CATATAN ATAS LAPORAN KEUANGAN KONSOLIDASIAN\nCatatan/ 1. UMUM\nJumlah liabilitas 1.000 2.000\n"


def test_vertical_layout_and_untitled_continuation_pages():
    p = parse_full_statements(["cover", V_BS1, V_BS2, V_PL, NOTES_PAGE])
    assert p.errors == []
    assert check_full_identities(p) == []
    g = lambda c: p.get(c, "consolidated", date(2026, 9, 30))  # noqa: E731
    assert g("total_liabilities") == Dec(102_240_852) * M
    assert g("equity_parent") == Dec(79_860_441) * M
    assert g("profit_before_tax") == Dec(15_263_686) * M
    assert g("income_tax") == Dec(3_390_262) * M
    assert g("net_income") == Dec(11_873_424) * M


def test_income_statement_pages_with_a_balance_sheet_title_are_recognised():
    mistitled = V_PL.replace(
        "LAPORAN LABA RUGI DAN PENGHASILAN\nKOMPREHENSIF LAIN KONSOLIDASIAN INTERIM",
        "LAPORAN POSISI KEUANGAN\nKONSOLIDASIAN INTERIM",
    )
    p = parse_full_statements(["cover", V_BS1, V_BS2, mistitled])
    assert p.errors == []
    assert p.get("net_income", "consolidated", date(2026, 9, 30)) == Dec(11_873_424) * M


# TLKM-like: Indonesian only, billions, tax as current + deferred without a total
T_BS = """PT TELEKOMUNIKASI CONTOH Tbk
LAPORAN POSISI KEUANGAN KONSOLIDASIAN
Tanggal 30 September 2026 (tidak diaudit) dan 31 Desember 2025 (diaudit)
(Angka dalam tabel disajikan dalam miliaran Rupiah, kecuali dinyatakan lain)
Catatan      30 September 2026      31 Desember 2025
Kas dan setara kas   3,32,37   33.626   34.228
JUMLAH ASET   279.814   270.000
JUMLAH LIABILITAS   139.352   135.000
Kepentingan nonpengendali   20.000   19.000
JUMLAH EKUITAS   140.462   135.000
"""
T_PL = """PT TELEKOMUNIKASI CONTOH Tbk
LAPORAN LABA RUGI DAN PENGHASILAN KOMPREHENSIF LAIN KONSOLIDASIAN
untuk Periode Sembilan Bulan yang Berakhir pada Tanggal 30 September 2026 dan 2025 (tidak diaudit)
(Angka dalam tabel disajikan dalam miliaran Rupiah, kecuali dinyatakan lain)
Catatan  2026  2025
LABA SEBELUM PAJAK PENGHASILAN    25.726   25.493
(BEBAN) MANFAAT PAJAK PENGHASILAN 27d
Pajak kini    (6.394)  (5.976)
Pajak tangguhan 2z.iii   130   325
LABA PERIODE BERJALAN    19.462   19.842
Laba periode berjalan yang dapat diatribusikan kepada:
Pemilik entitas induk   14.446   15.031
Kepentingan nonpengendali   5.016   4.811
"""


def test_indonesian_only_billions_and_tax_from_components():
    p = parse_full_statements(["cover", T_BS, T_PL])
    assert p.errors == []
    assert check_full_identities(p) == []
    g = lambda c: p.get(c, "consolidated", date(2026, 9, 30))  # noqa: E731
    assert g("total_assets") == Dec(279_814) * B
    assert g("income_tax") == Dec(6_264) * B  # 6,394 current - 130 deferred benefit
    assert g("net_income_parent") == Dec(14_446) * B


# UNVR-like: stand-alone statements, discontinued operations, EPS split by operation
U_BS = """PT CONTOH MANDIRI Tbk
Laporan Posisi Keuangan Interim
30 September 2026 dan 31 Desember 2025
(Dalam jutaan Rupiah, kecuali dinyatakan lain) (in millions of Rupiah, unless otherwise stated)
Jumlah aset 18,207,961   17,000,000  Total assets
Jumlah liabilitas 14,002,714   13,000,000  Total liabilities
Jumlah ekuitas 4,205,247   4,000,000  Total equity
"""
U_PL = """PT CONTOH MANDIRI Tbk
Laporan Laba Rugi dan
Penghasilan Komprehensif Lain Interim
Untuk Periode Sembilan Bulan yang Berakhir pada
30 September 2026 dan 2025
(Dalam jutaan Rupiah, kecuali dinyatakan lain) (In millions of Rupiah, unless otherwise stated)
LABA SEBELUM PAJAK       PROFIT BEFORE
PENGHASILAN 4,164,640   3,906,797  INCOME TAX
Beban pajak penghasilan (851,406) 2q, 14a (910,259) Income tax expense
LABA DARI OPERASI YANG    PROFIT FROM CONTINUING
DILANJUTKAN 3,313,234   2,996,538  OPERATION
Jumlah laba dari operasi yang dihentikan    Total profit from discontinued operation
setelah pajak 887,855  2y, 37 338,711  net of tax
LABA 4,201,089   3,335,249  PROFIT
LABA PER SAHAM DASAR     BASIC EARNINGS PER SHARE
Operasi yang dilanjutkan 87  79 Continuing operation
Operasi yang dihentikan 23  9 Discontinued operation
"""


def test_discontinued_operations_and_stand_alone_statements():
    p = parse_full_statements(["cover", U_BS, U_PL])
    assert p.errors == []
    assert check_full_identities(p) == []
    g = lambda c: p.get(c, "consolidated", date(2026, 9, 30))  # noqa: E731
    assert g("income_tax") == Dec(851_406) * M
    assert g("profit_continuing") == Dec(3_313_234) * M
    assert g("profit_discontinued") == Dec(887_855) * M
    assert g("net_income") == g("net_income_parent") == Dec(4_201_089) * M
    assert g("eps_basic") is None  # only per-operation EPS is printed: no total is invented


def test_identity_catches_a_wrong_continuing_split():
    bad = U_PL.replace("LABA 4,201,089", "LABA 4,301,089")
    p = parse_full_statements(["cover", U_BS, bad])
    assert any("continuing" in x for x in check_full_identities(p))


def test_unrecognised_layout_is_an_error_not_a_crash():
    parsed, problems, _ = parse_document(["just some text 30 tah 2026"])
    assert parsed.errors and problems == []


def test_encrypted_pdf_with_empty_user_password_is_read():
    from pypdf import PdfWriter

    from idx_trading.fundamentals.pdf import pdf_pages

    w = PdfWriter()
    w.add_blank_page(100, 100)
    w.encrypt(user_password="", owner_password="owner", algorithm="AES-256")
    buf = io.BytesIO()
    w.write(buf)
    assert pdf_pages(buf.getvalue()) == [""]
