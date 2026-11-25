"""Parser for full IDX financial statements (PSAK, bilingual), e.g. ANTAM.

Layout (observed, ADR 0012): each row is
    <Indonesian label> [note ref] <current> [note ref] <prior> <English label>
two columns (consolidated current / comparative). Labels may wrap over several lines.
Numbers are in English format ("10,513,725", "195.43"; ANTAM, INDF) or Indonesian format
("42.306.668", "317"; BBRI); the format is detected per document from the statement pages.
Dates, period length and units come from the statement titles or the column headers.

Rules: only anchored label patterns are mapped; a concept found twice in a
statement, or a missing title/unit, is an error. Accounting identities are
checked by the caller (`check_full_identities`) before anything is stored.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal

from idx_trading.fundamentals.bank_publication import ParsedFact, ParsedPublication, parse_date
from idx_trading.fundamentals.concepts import CONCEPTS, Kind

DOCUMENT_TYPE = "financial_statements"
_NUM_EN = re.compile(r"^\(?-?\d{1,3}(?:,\d{3})*(?:\.\d+)?\)?$|^-$")
_NUM_ID = re.compile(r"^\(?-?\d{1,3}(?:\.\d{3})*(?:,\d+)?\)?$|^-$")
_NUM = _NUM_EN  # module default (English); parse_full_statements switches per document
# note references: "5", "5,44", "2ac", "2am,2ao", "2z.iii", "2q," (followed by "14a")
_NOTE = re.compile(r"^\d{1,2}[a-z]{0,2}(?:\.[ivx]+)?(?:,\d{1,2}[a-z]{0,2}(?:\.[ivx]+)?)*,?$")
_PERIOD_MONTHS = {"tiga": 3, "enam": 6, "sembilan": 9}

_BS = [
    ("total_liabilities_and_equity", r"^(jumlah|total) liabilitas dan ekuitas total liabilities and equity$"),
    ("total_assets", r"^(jumlah|total) aset total assets$"),
    ("current_assets", r"^(jumlah|total) aset lancar total current assets$"),
    ("total_liabilities", r"^(jumlah|total) liabilitas total liabilities$"),
    ("current_liabilities", r"^(jumlah|total) liabilitas (jangka pendek|lancar) total current liabilities$"),
    ("total_equity", r"^(jumlah|total) ekuitas total equity$"),
    (
        "equity_parent",
        r"^(jumlah|total) ekuitas yang dapat diatribusikan .*(owners|holders) of the parent( entity)?$",
    ),
    ("equity_nci", r"^kepentingan non-?pengendali non-controlling interests?$"),
    ("cash", r"^kas dan setara kas cash and cash equivalents$"),
]
_PL = [
    ("revenue", r"^(penjualan|pendapatan)( bersih| usaha| neto)? (net )?(sales|revenues?)( net)?$"),
    ("revenue", r"^pendapatan (?=.*\bkontrak\b)(?=.*\brevenues? from contracts?\b).*customers?$"),
    ("gross_profit", r"^laba (kotor|bruto) gross profit$"),
    ("operating_profit", r"^laba usaha operating profit$"),
    # bilingual wrapping can interleave the languages ("laba sebelum income before beban pajak ...")
    ("profit_before_tax", r"^laba sebelum (?=.*\bbefore\b)(?=.*\bpajak\b).*\btax( expenses?)?$"),
    (
        "income_tax",
        r"^((jumlah|total) )?(beban|manfaat) pajak( penghasilan)?(, bersih| - neto)?"
        r" ((total) )?(income )?tax (expenses?|benefit)(, net| - net)?$",
    ),
    ("income_tax", r"^(beban|manfaat) pajak income tax penghasilan (expenses?|benefit)$"),
    ("net_income", r"^laba (bersih )?(periode|tahun) berjalan (net )?(profit|income) for the (period|year)$"),
    ("net_income", r"^laba bersih net income$"),
    ("net_income", r"^laba profit$"),  # UNVR: "LABA / PROFIT" after discontinued operations
    ("profit_continuing", r"^laba dari operasi yang (?=.*\bcontinuing\b).*dilanjutkan operations?$"),
    (
        "profit_discontinued",
        r"^(jumlah|total) laba dari operasi yang dihentikan .*discontinued operations?.*net of tax$",
    ),
]
# Indonesian-only labels, tried when no bilingual pattern matches: some layouts (INDF) put
# the English label on its own line *after* the numbers, so a row carries only Indonesian.
_BS_ID = [
    ("total_liabilities_and_equity", r"^(jumlah|total) liabilitas dan ekuitas$"),
    ("total_assets", r"^(jumlah|total) aset$"),
    ("current_assets", r"^(jumlah|total) aset lancar$"),
    ("total_liabilities", r"^(jumlah|total) liabilitas$"),
    ("current_liabilities", r"^(jumlah|total) liabilitas (jangka pendek|lancar)$"),
    ("total_equity", r"^(jumlah|total) ekuitas$"),
    ("equity_parent", r"^((jumlah|total) )?ekuitas yang dapat diatribusikan kepada pemilik entitas induk$"),
    ("equity_nci", r"^kepentingan non-?pengendali$"),
    ("cash", r"^kas dan setara kas$"),
]
_PL_ID = [
    ("revenue", r"^(penjualan|pendapatan)( bersih| usaha| neto)?$"),
    ("revenue", r"^pendapatan dari kontrak dengan pelanggan$"),
    ("gross_profit", r"^laba (kotor|bruto)$"),
    ("operating_profit", r"^laba usaha$"),
    ("profit_before_tax", r"^laba sebelum (beban )?pajak( penghasilan)?$"),
    ("income_tax", r"^((jumlah|total) )?(beban|manfaat) pajak( penghasilan)?(, bersih| - neto)?$"),
    ("net_income", r"^laba (bersih )?(periode|tahun) berjalan$"),
    ("net_income", r"^laba bersih$"),
]
# "(Disajikan dalam ...)", "(Dinyatakan dalam ...)", "(Dalam ...)" (UNVR),
# "(Angka dalam tabel disajikan dalam ...)" (TLKM)
_UNITS_RE = re.compile(r"\((?:disajikan |dinyatakan |angka dalam tabel disajikan )?dalam ([^)]*)\)")
_UNITS = [
    (r"miliaran rupiah", "IDR", 1_000_000_000),
    (r"jutaan rupiah", "IDR", 1_000_000),
    (r"ribuan rupiah", "IDR", 1_000),
    (r"ribuan dolar", "USD", 1_000),
    (r"dolar amerika serikat|dolar as\b", "USD", 1),
    (r"^rupiah\b", "IDR", 1),
]


def heal_numbers(line: str) -> str:
    """Re-join numbers that PDF text extraction split ("2,403 ,076", "1,338,42 7")."""
    s = re.sub(r"\(\s*([\d.,]+)\s*\)", r"(\1)", line)  # "(3.390.262  )"
    s = re.sub(r"(\d) ,(\d{3})\b", r"\1,\2", s)
    s = re.sub(r"(\d), (\d{3})\b", r"\1,\2", s)

    def join(m: re.Match[str]) -> str:
        return m.group(1) + m.group(2) if len(m.group(1)) - 1 + len(m.group(2)) == 3 else m.group(0)

    return re.sub(r"(,\d{1,2}) (\d{1,2})(?=\s|,|\)|$)", join, s)


def _value(tok: str, fmt: str = "en") -> Decimal:
    if tok == "-":
        return Decimal(0)
    neg = tok.startswith("(") and tok.endswith(")")
    body = tok.strip("()")
    body = body.replace(".", "").replace(",", ".") if fmt == "id" else body.replace(",", "")
    v = Decimal(body)
    return -v if neg else v


def number_format(pages: list[str]) -> str:
    """'id' if '1.234'-style numbers outnumber '1,234'-style ones, else 'en'.

    A single thousands group counts too: statements in billions (TLKM) rarely reach
    "1.234.567". English decimals have one or two digits ("195.43"), so ".ddd" is a group.
    """
    text = " ".join(pages)
    n_id = len(re.findall(r"(?<![\d.,])\d{1,3}(?:\.\d{3})+(?![\d.,]*\d)", text))
    n_en = len(re.findall(r"(?<![\d.,])\d{1,3}(?:,\d{3})+(?![\d.,]*\d)", text))
    return "id" if n_id > n_en else "en"


def _split_row(line: str, num: re.Pattern[str] = _NUM_EN) -> tuple[str, list[str]] | None:
    """(text without numbers and note refs, [current, prior]) or None if no value pair.

    A note reference may sit between the two value columns ("42.306.668 5,44 42.444.581",
    "5.311.301 2ac 6.854.545"): note-like tokens inside a run are transparent, and dropped
    from the middle of a run that would otherwise have more than two values.
    """
    toks = line.split()
    runs: list[list[int]] = []
    i = 0
    while i < len(toks):
        if num.match(toks[i]):
            run = [i]
            j = i + 1
            while j < len(toks) and (num.match(toks[j]) or _NOTE.match(toks[j])):
                run.append(j)
                j += 1
            while run and not num.match(toks[run[-1]]):
                run.pop()
            values = [k for k in run if num.match(toks[k])]
            middle = [k for k in values[1:-1] if _NOTE.match(toks[k])]
            while len(values) > 2 and middle:
                values.remove(middle.pop(0))
            runs.append(values)
            i = j
        else:
            i += 1
    runs = [r for r in runs if len(r) >= 2]
    if not runs:
        return None
    vals = runs[-1][-2:]
    span = set(range(runs[-1][0], runs[-1][-1] + 1))
    words = [t for k, t in enumerate(toks) if k not in span and not _NOTE.match(t)]
    return " ".join(words), [toks[k] for k in vals]


def _norm(text: str) -> str:
    t = text.lower().replace("\u2013", "-")  # en dash
    t = re.sub(r"[:]", " ", t)
    # sign-neutral captions: "LABA/(RUGI)", "(BEBAN)/MANFAAT", "PROFIT/(LOSS)", "(EXPENSE)/BENEFIT"
    for a, b in (
        ("laba/(rugi)", "laba"),
        ("(rugi)/laba", "laba"),
        ("(beban)/manfaat", "beban"),
        ("beban/(manfaat)", "beban"),
        ("profit/(loss)", "profit"),
        ("(loss)/profit", "profit"),
        ("(expense)/benefit", "expense"),
        ("expense/(benefit)", "expense"),
    ):
        t = t.replace(a, b)
    t = re.sub(r"\s+", " ", t).strip(" -")
    return re.sub(r"\s+", " ", t).strip()


def _units(text: str) -> tuple[str, int] | None:
    low = text.lower()
    m = _UNITS_RE.search(low)
    if not m:
        return None
    for pat, ccy, scale in _UNITS:
        if re.search(pat, m.group(1)):
            return ccy, scale
    return None


_ID_DATE = (
    r"\d{1,2} (?:januari|februari|maret|april|mei|juni|juli|agustus|september|oktober|november|desember)"
    r" \d{4}"
)


def _bs_dates(text: str) -> tuple[date, date] | None:
    low = text.lower()
    m = re.search(r"(\d{1,2} [a-z]+ \d{4}) dan (\d{1,2} [a-z]+ \d{4})", low)
    if m:
        return parse_date(m.group(1)), parse_date(m.group(2))
    # Title gives one date; the column headers give both ("30 September 2026/ ... 31 Desember 2025/").
    head = "\n".join(low.splitlines()[:45])
    seen: list[date] = []
    for d in re.findall(_ID_DATE, head):
        pd_ = parse_date(d)
        if pd_ not in seen:
            seen.append(pd_)
    if len(seen) == 2 and seen[0] > seen[1]:
        return seen[0], seen[1]
    # Headers split day/month from the year ("30 September/ ... 31 Desember/" over
    # "September 2026 ... December 2025"): the title date is current, the second header's
    # day and month take the other year printed in the header.
    months = _ID_DATE.split(" (?:", 1)[1].split(")", 1)[0]
    dm = re.findall(r"(\d{1,2}) (" + months + r")\s*/", head)
    years = sorted({int(y) for y in re.findall(r"\b(20\d{2})\b", head)})
    if len(seen) == 1 and (seen[0].month, seen[0].day) == (12, 31) and seen[0].year - 1 in years:
        # annual statements: "Tanggal 31 Desember 2025" over year columns "2025 ... 2024"
        return seen[0], seen[0].replace(year=seen[0].year - 1)
    if len(seen) == 1 and len(dm) >= 2 and len(years) >= 2:
        current = seen[0]
        for y in reversed(years):  # latest year that puts the comparative before the current date
            try:
                prior = parse_date(f"{dm[1][0]} {dm[1][1]} {y}")
            except ValueError:
                return None
            if prior < current:
                return current, prior
    return None


def _pl_dates(text: str) -> tuple[date, date, int] | None:
    low = re.sub(r"\s+", " ", text.lower())
    m = re.search(r"berakhir (?:pada )?(?:tanggal )?(\d{1,2} [a-z]+ (\d{4})) dan (\d{4})", low)
    if m:
        end = parse_date(m.group(1))
        prev = end.replace(year=int(m.group(3)))
    else:
        # "... yang Berakhir pada Tanggal 30 September 2026" + year columns "2026 ... 2025"
        m2 = re.search(r"berakhir pada tanggal (" + _ID_DATE + ")", low) or re.search(
            r"tanggal (" + _ID_DATE + ")", low
        )
        if not m2:
            return None
        end = parse_date(m2.group(1))
        head = " ".join(text.splitlines()[:45])
        if not re.search(rf"\b{end.year - 1}\b", head):
            return None
        prev = end.replace(year=end.year - 1)
    pm = re.search(r"(tiga|enam|sembilan) bulan", low)
    months = _PERIOD_MONTHS[pm.group(1)] if pm else (12 if "tahun yang berakhir" in low else 0)
    if months == 0 and re.search(r"periode yang berakhir", low) and end.month in (3, 6, 9):
        # "untuk periode yang berakhir 30 September" without a length (ASII): IDX interim
        # statements are year-to-date, and these issuers' fiscal years are calendar years.
        months = end.month
    if months == 0:
        raise ValueError("cannot tell the length of the income-statement period")
    return end, prev, months


def _period_start(end: date, months: int) -> date:
    y, mth = end.year, end.month - months + 1
    while mth <= 0:
        mth += 12
        y -= 1
    return date(y, mth, 1)


def _is_statement_page(page: str, title: str) -> bool:
    """A primary statement page has the title as a line near the top and a units line.

    Notes pages mention "laporan posisi keuangan" in running text and must not count.
    """
    top = [s for ln in page.splitlines() if (s := ln.strip().upper())][:25]
    return any(ln.startswith(title) for ln in top) and _UNITS_RE.search(page.lower()) is not None


def _has_period_header(page: str) -> bool:
    head = re.sub(r"\s+", " ", " ".join(page.splitlines()[:45]).lower())
    return bool(re.search(r"periode (tiga|enam|sembilan) bulan yang berakhir|tahun yang berakhir", head))


def _first_block(pages: list[str], title: str) -> list[str]:
    """The first run of consecutive statement pages with `title`.

    Later runs are other statements with the same title, e.g. the parent-only statements
    that banks attach as supplementary information; they must not be mixed in.
    """
    idx = [i for i, p in enumerate(pages) if _is_statement_page(p, title)]
    if not idx:
        return []
    block = [idx[0]]
    i = idx[0] + 1
    while i < len(pages) and (i in idx or _is_untitled_continuation(pages[i])):
        block.append(i)
        i += 1
    return [pages[i] for i in block]


_TITLES = (
    "LAPORAN POSISI KEUANGAN",
    "LAPORAN LABA RUGI",
    "LAPORAN PERUBAHAN EKUITAS",
    "LAPORAN ARUS KAS",
    "CATATAN ATAS LAPORAN",
)


def _is_untitled_continuation(page: str) -> bool:
    """A statement page without its title (INDF prints the title on the first page only):
    no statement or notes title near the top, but the "Catatan/ Notes" column header.
    The disclaimer "Catatan atas laporan keuangan ... terlampir merupakan ..." heads every
    statement page and is not a title."""
    top = [s for ln in page.splitlines() if (s := ln.strip().upper())][:25]
    if any(ln.startswith(t) and "TERLAMPIR" not in ln for t in _TITLES for ln in top):
        return False
    return any(ln.upper().startswith("CATATAN/") for ln in top)


def _shares_issued(bs_text: str) -> Decimal | None:
    """Issued ordinary shares from the share-capital caption.

    Indonesian figures use '.' thousands ("24.030.764.724 saham biasa"); the bilingual
    layout interleaves English words, so only "ditempatkan ... <n> saham biasa" is required.
    """
    flat = re.sub(r"\s+", " ", bs_text.lower())
    m = re.search(r"ditempatkan.{0,300}?(\d{1,3}(?:\.\d{3}){2,}) saham biasa", flat) or re.search(
        r"ditempatkan dan disetor.{0,80}?(\d{1,3}(?:\.\d{3}){2,}) (?:lembar|saham)", flat
    )
    return Decimal(m.group(1).replace(".", "")) if m else None


def parse_full_statements(pages: list[str]) -> ParsedPublication:
    errors: list[str] = []
    facts: list[ParsedFact] = []
    bs_pages = _first_block(pages, "LAPORAN POSISI KEUANGAN")
    pl_pages = _first_block(pages, "LAPORAN LABA RUGI")
    if bs_pages and not pl_pages:
        # Some issuers (ICBP 2026-09) print the balance-sheet title on the income-statement
        # pages too; those pages have a period header ("periode ... bulan yang berakhir").
        pl_pages = [p for p in bs_pages if _has_period_header(p)]
        bs_pages = [p for p in bs_pages if not _has_period_header(p)]
    if not bs_pages or not pl_pages:
        out = ParsedPublication(date.min, None)
        out.errors.append("no balance sheet / income statement titles found")
        return out
    fmt = number_format(bs_pages + pl_pages)
    num = _NUM_ID if fmt == "id" else _NUM_EN
    units = _units(bs_pages[0])
    bs_dates = _bs_dates(bs_pages[0])
    pl: tuple[date, date, int] | None = None
    try:
        pl = _pl_dates(pl_pages[0])
    except ValueError as exc:
        errors.append(str(exc))
    if units is None:
        errors.append("units line '(Disajikan dalam ...)' not found")
    if bs_dates is None:
        errors.append("balance-sheet dates not found in the title")
    if pl is None and not errors:
        errors.append("income-statement period not found in the title")
    out = ParsedPublication(bs_dates[0] if bs_dates else date.min, None)
    out.errors = errors
    if errors:
        return out
    assert units and bs_dates and pl
    ccy, scale = units
    if pl[0] != bs_dates[0]:
        out.errors.append(f"income statement ends {pl[0]}, balance sheet {bs_dates[0]}")
        return out
    head = " ".join(pages[: max(1, pages.index(bs_pages[0]) + 1)]).lower()
    out.audited = (
        False
        if re.search(r"tidak diaudit|unaudited", head)
        else (True if re.search(r"\bdiaudit\b|\baudited\b", head) else None)
    )

    def emit(concept: str, raw: list[str], statement: str, label: str) -> None:
        kind = CONCEPTS[concept].kind
        unit_scale = 1 if concept == "eps_basic" else scale
        ends = bs_dates if statement == "balance" else (pl[0], pl[1])
        for col, end in enumerate(ends):
            start = _period_start(end, pl[2]) if kind == Kind.DURATION else None
            facts.append(
                ParsedFact(
                    concept,
                    "consolidated",
                    start,
                    end,
                    _value(raw[col], fmt) * unit_scale,
                    unit_scale,
                    statement,
                    label[:300],
                )
            )

    def lines_of(page: str) -> list[str]:
        """Healed lines; consecutive lines holding only numbers/note refs are joined, so a
        vertical layout ("36" / "102.240.852" / "97.746.518") becomes one value row."""
        out_lines: list[str] = []
        prev_numeric = False
        for raw_line in page.splitlines():
            line = heal_numbers(raw_line.strip())
            if not line:
                continue  # blank lines carry no structure in extracted text
            toks = line.split()
            numeric = bool(toks) and all(num.match(x) or _NOTE.match(x) for x in toks)
            if numeric and prev_numeric and out_lines:
                out_lines[-1] += " " + line
            else:
                out_lines.append(line)
            prev_numeric = numeric
        return out_lines

    def walk(
        statement_pages: list[str],
        patterns: list[tuple[str, str]],
        statement: str,
        fallback: list[tuple[str, str]],
    ) -> None:
        seen: set[str] = set()
        buf: list[str] = []
        attrib = False
        for page in statement_pages:
            for line in lines_of(page):
                if not line:
                    buf = []
                    continue
                split = _split_row(line, num)
                if split is None:
                    label = " ".join(x for x in line.split() if not _NOTE.match(x))
                    if label:
                        buf.append(_norm(label))
                    if statement == "income":
                        joined = " ".join(buf)
                        # whichever heading came last decides: profit attribution or not
                        stop = max(joined.rfind(w) for w in ("komprehensif", "comprehensive", "per saham"))
                        if "diatribusikan" in joined and joined.rfind("laba") > stop:
                            attrib = True
                        elif stop >= 0:
                            attrib = False
                    continue
                text, raw = split
                line_text = _norm(text)
                # A label may wrap over the lines above it, and section headings
                # ("ASET LANCAR / CURRENT ASSETS") also sit there. Patterns are anchored,
                # so a heading + label rarely matches: the widest matching candidate wins,
                # which keeps "LABA SEBELUM / BEBAN PAJAK <n>" from reading as tax.
                candidates = [_norm(" ".join([*buf[k:], line_text])) for k in range(len(buf), -1, -1)]
                row_text = candidates[-1]
                buf = []
                if not row_text:
                    continue
                concept = None
                for pats in (patterns, fallback):
                    for cand in reversed(candidates):
                        concept = next((name for name, pat in pats if re.search(pat, cand)), None)
                        if concept is not None:
                            break
                    if concept is not None:
                        break
                if statement == "income":
                    owners = r"pemilik entitas induk( (owners|equity holders) of the parent( entity)?)?"
                    if attrib and re.fullmatch(owners, line_text):
                        concept = "net_income_parent"
                    elif attrib and re.fullmatch(
                        r"kepentingan non-?pengendali( non-controlling interests?)?", line_text
                    ):
                        concept = "net_income_nci"
                    elif "operasi yang" in line_text and concept is None:
                        pass  # EPS split by continuing/discontinued operations: no total EPS
                    elif re.search(r"\blaba (bersih )?per saham\b", row_text) or (
                        re.search(r"\bper saham\b", row_text)
                        and re.fullmatch(r"(dasar|basic|dasar basic)", line_text)
                    ):
                        concept = "eps_basic"
                    if concept is None and re.fullmatch(
                        r"((beban )?pajak )?(kini|tangguhan)( (current|deferred)( tax)?)?", line_text
                    ):
                        tax_parts.append((line_text, raw))
                if concept is None:
                    continue
                if concept in seen:
                    errors.append(f"{concept}: matched twice in the {statement} statement")
                    continue
                seen.add(concept)
                emit(concept, raw, statement, row_text)

    tax_parts: list[tuple[str, list[str]]] = []
    walk(bs_pages, _BS, "balance", _BS_ID)
    walk(pl_pages, _PL, "income", _PL_ID)
    have = {f.concept for f in facts}
    kinds = sorted("kini" if "kini" in lbl else "tangguhan" for lbl, _ in tax_parts)
    if "income_tax" not in have and kinds == ["kini", "tangguhan"]:
        # TLKM prints current and deferred tax without a total: their sum is the tax line
        # (still checked by PBT - tax = NI below).
        for col, end in enumerate((pl[0], pl[1])):
            total = sum((_value(r[col], fmt) for _, r in tax_parts), Decimal(0)) * scale
            facts.append(
                ParsedFact(
                    "income_tax",
                    "consolidated",
                    _period_start(end, pl[2]),
                    end,
                    total,
                    scale,
                    "income",
                    "pajak kini + pajak tangguhan (no total row printed)",
                )
            )
    consolidated = any("KONSOLIDASI" in p.upper()[:3000] for p in bs_pages[:1])
    if not consolidated and "net_income_parent" not in have:
        # Stand-alone statements (UNVR has no subsidiaries): all profit belongs to the owners.
        for f in [f for f in facts if f.concept == "net_income"]:
            facts.append(
                ParsedFact(
                    "net_income_parent",
                    f.basis,
                    f.period_start,
                    f.period_end,
                    f.value,
                    f.scale,
                    "income",
                    "laba (entitas tanpa entitas anak: seluruhnya milik pemilik)",
                )
            )

    shares = _shares_issued(" ".join(bs_pages))
    if shares is not None:
        for end in bs_dates:
            facts.append(
                ParsedFact(
                    "shares_issued",
                    "consolidated",
                    None,
                    end,
                    shares,
                    1,
                    "balance",
                    "modal ditempatkan dan disetor penuh (saham biasa)",
                )
            )
    out.facts = facts
    out.errors = errors
    out.currency = ccy
    out.document_type = DOCUMENT_TYPE
    _normalise_tax(out)
    return out


def _normalise_tax(p: ParsedPublication) -> None:
    """Statements print the tax expense as a negative number; store it as an expense (+).

    The sign is decided by the identity PBT - tax = NI, never assumed.
    """
    fixed: list[ParsedFact] = []
    for f in p.facts:
        if f.concept != "income_tax":
            fixed.append(f)
            continue
        pbt = p.get("profit_before_tax", f.basis, f.period_end)
        # with discontinued operations, PBT - tax = profit from continuing operations
        ni = p.get("profit_continuing", f.basis, f.period_end) or p.get("net_income", f.basis, f.period_end)
        value = f.value
        if pbt is not None and ni is not None:
            tol = Decimal(2 * f.scale)
            if abs(pbt + f.value - ni) <= tol:
                value = -f.value
            elif abs(pbt - f.value - ni) > tol:
                p.errors.append(f"income tax {f.value} fits neither sign for PBT {pbt} and NI {ni}")
        fixed.append(
            ParsedFact(
                f.concept, f.basis, f.period_start, f.period_end, value, f.scale, f.statement, f.source_label
            )
        )
    p.facts = fixed


def check_full_identities(p: ParsedPublication) -> list[str]:
    problems: list[str] = []
    need = (
        "total_assets",
        "total_liabilities",
        "total_equity",
        "profit_before_tax",
        "income_tax",
        "net_income",
        "net_income_parent",
    )
    present = {f.concept for f in p.facts}
    problems += [f"missing required concept {c}" for c in need if c not in present]
    if problems:
        return problems
    scale = max((f.scale for f in p.facts if f.concept == "total_assets"), default=1)
    tol = Decimal(2 * scale)
    ends = sorted({f.period_end for f in p.facts})
    for end in ends:

        def g(c: str, end: date = end) -> Decimal | None:
            return p.get(c, "consolidated", end)

        ta, tl, te, tle = (
            g("total_assets"),
            g("total_liabilities"),
            g("total_equity"),
            g("total_liabilities_and_equity"),
        )
        if ta is not None and tl is not None and te is not None:
            if abs(tl + te - ta) > tol:
                problems.append(f"{end}: liabilities {tl} + equity {te} != assets {ta}")
            if tle is not None and abs(tle - ta) > tol:
                problems.append(f"{end}: total liabilities and equity {tle} != assets {ta}")
            ep, en = g("equity_parent"), g("equity_nci")
            if ep is not None and en is not None and abs(ep + en - te) > tol:
                problems.append(f"{end}: parent {ep} + NCI {en} equity != total {te}")
        pbt, tax, ni, nip = g("profit_before_tax"), g("income_tax"), g("net_income"), g("net_income_parent")
        if None not in (pbt, tax, ni, nip):
            assert pbt is not None and tax is not None and ni is not None and nip is not None
            cont, disc = g("profit_continuing"), g("profit_discontinued")
            after_tax = cont if cont is not None else ni
            if abs(pbt - tax - after_tax) > tol:
                problems.append(f"{end}: PBT {pbt} - tax {tax} != profit after tax {after_tax}")
            if cont is not None and abs(cont + (disc or Decimal(0)) - ni) > tol:
                problems.append(f"{end}: continuing {cont} + discontinued {disc} != NI {ni}")
            nci = g("net_income_nci") or Decimal(0)
            if abs(nip + nci - ni) > tol:
                problems.append(f"{end}: owners {nip} + NCI {nci} != NI {ni}")
    return problems


def eps_share_check(p: ParsedPublication) -> list[str]:
    """Warnings (not errors): reported EPS vs owners' profit / issued shares."""
    out = []
    for f in p.facts:
        if f.concept != "eps_basic" or f.value == 0:
            continue
        nip = p.get("net_income_parent", "consolidated", f.period_end)
        sh = p.get("shares_issued", "consolidated", p.period_end)
        if nip is None or not sh:
            continue
        implied = nip / sh
        if abs(implied / f.value - 1) > Decimal("0.01"):
            out.append(
                f"EPS {f.value} vs profit/shares {implied:.2f} for {f.period_end} (treasury or new shares?)"
            )
    return out


def parse_document(pages: list[str]) -> tuple[ParsedPublication, list[str], list[str]]:
    """Detect the format and parse. Returns (parsed, identity problems, warnings)."""
    from idx_trading.fundamentals.bank_publication import (
        check_identities,
        parse_publication,
    )

    if any(_is_statement_page(p, "LAPORAN POSISI KEUANGAN") for p in pages):
        parsed = parse_full_statements(pages)
        problems = [] if parsed.errors else check_full_identities(parsed)
        return parsed, problems, ([] if parsed.errors or problems else eps_share_check(parsed))
    try:
        parsed = parse_publication(pages)
    except ValueError as exc:  # neither layout: report, never crash the pipeline
        out = ParsedPublication(date.min, None)
        out.errors.append(f"unrecognised statement layout ({exc})")
        return out, [], []
    return parsed, ([] if parsed.errors else check_identities(parsed)), []
