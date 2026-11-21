"""A fake EODHD server for tests. Response shapes copied from the official docs."""

from __future__ import annotations

import json
import urllib.parse
from collections.abc import Mapping


class FakeEodhd:
    def __init__(self) -> None:
        self.bars: dict[str, list[dict]] = {}  # "BBCA.JK" -> rows (ascending)
        self.calls: list[str] = []
        self.status_override: dict[str, int] = {}  # path suffix -> status
        self.fail_first: int = 0  # transient 503s before succeeding
        self.symbols: list[dict] = []
        self.delisted: list[dict] = []
        self.dividends: dict[str, list[dict]] = {}  # "BBCA.JK" -> /div rows
        self.splits: dict[str, list[dict]] = {}  # "BBCA.JK" -> /splits rows

    def add(self, symbol: str, date: str, o, h, lo, c, v, adj=None) -> None:
        self.bars.setdefault(symbol, []).append(
            {
                "date": date,
                "open": o,
                "high": h,
                "low": lo,
                "close": c,
                "adjusted_close": adj if adj is not None else c,
                "volume": v,
            }
        )

    def __call__(self, url: str, headers: Mapping[str, str], timeout: float):
        parsed = urllib.parse.urlparse(url)
        q = dict(urllib.parse.parse_qsl(parsed.query))
        self.calls.append(parsed.path)
        assert headers["User-Agent"].startswith("idx-trading/")
        assert "python-urllib" not in headers["User-Agent"] and "Mozilla" not in headers["User-Agent"]
        if q.get("api_token") != "test-token":
            return 401, {}, b"Unauthenticated"
        if self.fail_first:
            self.fail_first -= 1
            return 503, {"Retry-After": "1"}, b"busy"
        for suffix, status in self.status_override.items():
            if parsed.path.endswith(suffix):
                return status, {}, b"Ticker Not Found."
        ctype = {"Content-Type": "application/json; charset=utf-8"}
        if "/eod-bulk-last-day/" in parsed.path:
            ex = parsed.path.rsplit("/", 1)[-1]
            rows = [
                {"code": sym.split(".")[0], "exchange_short_name": ex, **r}
                for sym, rs in self.bars.items()
                if sym.endswith("." + ex)
                for r in rs
                if r["date"] == q["date"]
            ]
            return 200, ctype, json.dumps(rows).encode()
        if "/exchange-symbol-list/" in parsed.path:
            data = self.delisted if q.get("delisted") == "1" else self.symbols
            return 200, ctype, json.dumps(data).encode()
        for prefix, store in (("/div/", self.dividends), ("/splits/", self.splits)):
            if prefix in parsed.path:
                sym = parsed.path.rsplit("/", 1)[-1]
                rows = [
                    r for r in store.get(sym, []) if q.get("from", "0000") <= r["date"] <= q.get("to", "9999")
                ]
                return 200, ctype, json.dumps(rows).encode()
        if "/eod/" in parsed.path:
            sym = parsed.path.rsplit("/", 1)[-1]
            rows = [
                r for r in self.bars.get(sym, []) if q.get("from", "0000") <= r["date"] <= q.get("to", "9999")
            ]
            return 200, ctype, json.dumps(rows).encode()
        return 404, {}, b"not found"


def fmt_id(v: int) -> str:
    """Indonesian number format as printed in bank publication reports."""
    if v == 0:
        return "-"
    s = f"{abs(v):,}".replace(",", ".")
    return f"({s})" if v < 0 else s


_ID_MONTH = {3: "Mar", 6: "Jun", 9: "Sep", 12: "Des"}


def bank_publication_pages(
    end,
    *,
    prev_bs,
    prev_pl,
    ta,
    tl,
    te,
    ni,
    nip,
    nci,
    pbt,
    eps,
    eps_prev,
    scale_prev=0.9,
    bank_only_factor=0.97,
    audited=False,
):
    """Synthetic OJK publication report text (two statements) with self-consistent numbers.

    Columns: individual current, individual prior, consolidated current, consolidated prior.
    All amounts in Rp million, like the real report.
    """

    def d(x):
        return f"{x.day} {_ID_MONTH[x.month]} {x.year}"

    def cols(v):
        ind = int(v * bank_only_factor)
        return [ind, int(ind * scale_prev), v, int(v * scale_prev)]

    def row(label, v):
        return label + " " + "   ".join(fmt_id(x) for x in cols(v))

    status = "Diaudit" if audited else "Tidak Diaudit"
    # Keep the identities exact in every column: derive totals from parts per column.
    ta_c, tl_c, te_c = cols(ta), cols(tl), cols(te)
    tle_c = [a + b for a, b in zip(tl_c, te_c, strict=True)]
    bs = "\n".join(
        [
            f"{status} Diaudit {status} Diaudit",
            f"{d(end)} {d(prev_bs)} {d(end)} {d(prev_bs)}",
            "ASET",
            "1. Kas 1.000   1.000   1.000   1.000",
            row("9. Kredit dan pembiayaan yang diberikan", int(ta * 0.6)),
            "TOTAL ASET " + "   ".join(fmt_id(x) for x in tle_c),
            "LIABILITAS DAN EKUITAS",
            "LIABILITAS",
            row("1. Giro", int(tl * 0.3)),
            row("2. Tabungan", int(tl * 0.4)),
            row("3. Deposito", int(tl * 0.15)),
            "TOTAL LIABILITAS " + "   ".join(fmt_id(x) for x in tl_c),
            "EKUITAS",
            "TOTAL EKUITAS " + "   ".join(fmt_id(x) for x in te_c),
            "TOTAL LIABILITAS DAN EKUITAS " + "   ".join(fmt_id(x) for x in tle_c),
        ]
    )
    del ta_c
    ni_c, nci_c = cols(ni), [0, 0, nci, int(nci * scale_prev)]
    nip_c = [ni_c[0], ni_c[1], ni_c[2] - nci_c[2], ni_c[3] - nci_c[3]]
    pbt_c = cols(pbt)
    tax_c = [p - n for p, n in zip(pbt_c, ni_c, strict=True)]

    def line(label, values):
        return label + " " + "   ".join(fmt_id(x) for x in values)

    del nip
    pl = "\n".join(
        [
            f"{status} {status} {status} {status}",
            f"{d(end)} {d(prev_pl)} {d(end)} {d(prev_pl)}",
            "PENDAPATAN DAN BEBAN OPERASIONAL",
            row("Pendapatan (Beban) Bunga Bersih", int(pbt * 1.1)),
            row("LABA (RUGI) OPERASIONAL", pbt),
            line("LABA (RUGI) TAHUN BERJALAN SEBELUM PAJAK", pbt_c),
            line("Pajak penghasilan", tax_c),
            line("LABA (RUGI) BERSIH TAHUN BERJALAN", ni_c),
            "Laba (Rugi) Bersih Tahun Berjalan yang dapat diatribusikan",
            "kepada :",
            line("PEMILIK", nip_c),
            f"KEPENTINGAN NON-PENGENDALI {fmt_id(nci_c[2])}   {fmt_id(nci_c[3])}",
            line("TOTAL LABA (RUGI) BERSIH TAHUN BERJALAN", ni_c),
            "Total Laba (Rugi) Komprehensif Tahun Berjalan yang dapat",
            "diatribusikan kepada :",
            line("PEMILIK", nip_c),
            f"LABA BERSIH PER SAHAM (dalam satuan Rupiah) {eps}   {eps_prev}",
        ]
    )
    return [bs, pl, "I. ARUS KAS DARI AKTIVITAS OPERASI\nirrelevant 1 2"]
