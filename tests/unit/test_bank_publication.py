from datetime import date
from decimal import Decimal as Dec

import pytest

from idx_trading.fundamentals.adapters import parse_bca_index
from idx_trading.fundamentals.bank_publication import (
    check_identities,
    parse_date,
    parse_number,
    parse_publication,
)
from tests.fakes import bank_publication_pages

M = 1_000_000


def pages(**kw):
    base = dict(
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
    base.update(kw)
    return bank_publication_pages(**base)


@pytest.mark.parametrize(
    ("tok", "v"),
    [("1.590.090.741", 1590090741), ("(30.948.109)", -30948109), ("-", 0), ("119", 119), ("5.676", 5676)],
)
def test_parse_number(tok, v):
    assert parse_number(tok) == v


def test_parse_date_indonesian_months():
    assert parse_date("31 Des 2025") == date(2025, 12, 31)
    assert parse_date("Pada Tanggal 31 Maret 2026") == date(2026, 3, 31)
    assert parse_date("30 Jun 2026") == date(2026, 6, 30)
    with pytest.raises(ValueError):
        parse_date("31 Foo 2026")


def test_parse_synthetic_report():
    r = parse_publication(pages())
    assert r.errors == [] and r.period_end == date(2026, 3, 31) and r.audited is False
    assert check_identities(r) == []
    assert r.get("total_equity", "consolidated", date(2026, 3, 31)) == Dec(150_000 * M)
    assert r.get("total_equity", "consolidated", date(2025, 12, 31)) == Dec(135_000 * M)
    # Duration facts carry period_start = 1 Jan; balance-sheet facts none.
    ni = next(
        f
        for f in r.facts
        if f.concept == "net_income" and f.basis == "consolidated" and f.period_end == date(2025, 3, 31)
    )
    assert ni.period_start == date(2025, 1, 1) and ni.value == Dec(9_000 * M)
    assert next(f for f in r.facts if f.concept == "total_assets").period_start is None
    # EPS: whole rupiah, consolidated only; NCI: consolidated only.
    assert r.get("eps_basic", "consolidated", date(2026, 3, 31)) == 119
    assert r.get("eps_basic", "individual", date(2026, 3, 31)) is None
    assert r.get("net_income_nci", "consolidated", date(2026, 3, 31)) == Dec(10 * M)
    # Owners' line comes from the net-profit block, not the comprehensive-income block.
    assert r.get("net_income_parent", "consolidated", date(2026, 3, 31)) == Dec(9_990 * M)


def test_identity_failure_is_detected():
    p = pages()
    p[0] = p[0].replace("TOTAL EKUITAS 145.500", "TOTAL EKUITAS 145.400")
    r = parse_publication(p)
    problems = check_identities(r)
    assert any("liabilities" in x for x in problems)


def test_truncated_row_and_duplicate_label_are_errors():
    p = pages()
    p[0] = p[0].replace(
        "TOTAL LIABILITAS 824.500",
        "TOTAL LIABILITAS 824.500\nTOTAL LIABILITAS 1   2   3   4",
    )
    p[1] = p[1].replace("LABA (RUGI) OPERASIONAL", "LABA (RUGI) OPERASIONAL 12.125   10.912\nXX")
    r = parse_publication(p)
    assert any("appears twice" in e for e in r.errors)
    assert any("expected 4 columns" in e for e in r.errors)


def test_missing_statements():
    r = parse_publication(["nothing here"])
    assert r.errors and "no recognisable" in r.errors[0]


def test_parse_bca_index_reads_embedded_list_and_dates():
    def item(title, url):
        return (
            f'{{\\"id\\":\\"X\\",\\"title\\":\\"{title}\\",\\"description\\":\\"\\",\\"size\\":\\"1 KB\\",'
            f'\\"sizeBytes\\":1,\\"year\\":2026,\\"url\\":\\"{url}\\",\\"file_thumbnail_url\\":\\"\\"}}'
        )

    base = "https://pustaka.example/2026/"
    html = "".join(
        [
            item("Laporan Keuangan Maret 2026", base + "20260423-laporan-keuangan-maret-2026.pdf"),
            item("Laporan Keuangan Des 2025", base + "20260127-laporan-keuangan-desember-2025.pdf"),
            item("Laporan Keuangan Juni 2026", base + "20261116_20260728-laporan-keuangan-juni-2026.pdf"),
            item("Laporan Keuangan Publikasi Bulanan Mei 2026", base + "20260615-bulanan.pdf"),
            item("Laporan Keuangan Diaudit PT BCA Tbk Desember 2025", base + "20260127-audited.pdf"),
            item("Laporan Keuangan September 2026", base + "no-date-in-name.pdf"),
        ]
    )
    links = parse_bca_index(html)
    assert [lk.title for lk in links] == [
        "Laporan Keuangan Des 2025",
        "Laporan Keuangan Maret 2026",
        "Laporan Keuangan Juni 2026",
    ]
    juni = links[-1]
    assert juni.published_on == date(2026, 11, 16) and juni.original_published_on == date(2026, 7, 28)


def test_pdf_pages_reads_real_pdf_and_rejects_garbage():
    import io

    from pypdf import PdfWriter

    from idx_trading.fundamentals.pdf import pdf_pages

    buf = io.BytesIO()
    w = PdfWriter()
    w.add_blank_page(width=200, height=200)
    w.add_blank_page(width=200, height=200)
    w.write(buf)
    assert pdf_pages(buf.getvalue()) == ["", ""]
    with pytest.raises(ValueError, match="unreadable PDF"):
        pdf_pages(b"%PDF-1.7 this is not really a pdf")


def test_interim_label_variant_periode_berjalan():
    p = pages()
    p[1] = p[1].replace("TAHUN BERJALAN", "PERIODE BERJALAN").replace("Tahun Berjalan", "Periode Berjalan")
    r = parse_publication(p)
    assert r.errors == [] and check_identities(r) == []
    assert r.get("net_income_parent", "consolidated", date(2026, 3, 31)) == Dec(9_990 * M)
