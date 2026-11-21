"""Parser for full IDX financial statements (PSAK, bilingual), e.g. ANTAM.

Layout (observed, ADR 0012): each row is
    <Indonesian label> [note ref] <current> <prior> <English label>
in English number format ("10,513,725", "(50,782,060)", "195.43"), two columns
(consolidated current / comparative). Labels may wrap over several lines.
Dates, period length and units come from the statement titles.

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
_NUM = re.compile(r"^\(?-?\d{1,3}(?:,\d{3})*(?:\.\d+)?\)?$|^-$")
_NOTE = re.compile(r"^\d{1,2}[a-z]?(?:,\d{1,2}[a-z]?)*$|^\d{1,2}[a-z]$")
_PERIOD_MONTHS = {"tiga": 3, "enam": 6, "sembilan": 9}

_BS = [
    ("total_liabilities_and_equity", r"^jumlah liabilitas dan ekuitas total liabilities and equity$"),
    ("total_assets", r"^(jumlah|total) aset total assets$"),
    ("current_assets", r"^jumlah aset lancar total current assets$"),
    ("total_liabilities", r"^jumlah liabilitas total liabilities$"),
    ("current_liabilities", r"^jumlah liabilitas (jangka pendek|lancar) total current liabilities$"),
    ("total_equity", r"^jumlah ekuitas total equity$"),
    ("equity_parent", r"^jumlah ekuitas yang dapat diatribusikan kepada pemilik .*owners of the parent$"),
    ("equity_nci", r"^kepentingan non-?pengendali non-controlling interests?$"),
    ("cash", r"^kas dan setara kas cash and cash equivalents$"),
]
_PL = [
    ("revenue", r"^(penjualan|pendapatan)( bersih| usaha| neto)? (net )?(sales|revenues?)( net)?$"),
    ("gross_profit", r"^laba kotor gross profit$"),
    ("operating_profit", r"^laba usaha operating profit$"),
    ("profit_before_tax", r"^laba sebelum (beban )?pajak penghasilan profit before income tax( expense)?$"),
    ("income_tax", r"^(beban|manfaat) pajak penghasilan(, bersih)? income tax (expense|benefit)(, net)?$"),
    ("net_income", r"^laba (bersih )?(periode|tahun) berjalan (net )?profit for the (period|year)$"),
]
_UNITS = [
    (r"jutaan rupiah", "IDR", 1_000_000),
    (r"ribuan rupiah", "IDR", 1_000),
    (r"ribuan dolar", "USD", 1_000),
    (r"dolar amerika serikat|dolar as\b", "USD", 1),
    (r"dalam rupiah", "IDR", 1),
]


def heal_numbers(line: str) -> str:
    """Re-join numbers that PDF text extraction split ("2,403 ,076", "1,338,42 7")."""
    s = re.sub(r"(\d) ,(\d{3})\b", r"\1,\2", line)
    s = re.sub(r"(\d), (\d{3})\b", r"\1,\2", s)

    def join(m: re.Match[str]) -> str:
        return m.group(1) + m.group(2) if len(m.group(1)) - 1 + len(m.group(2)) == 3 else m.group(0)

    return re.sub(r"(,\d{1,2}) (\d{1,2})(?=\s|,|\)|$)", join, s)


def _value(tok: str) -> Decimal:
    if tok == "-":
        return Decimal(0)
    neg = tok.startswith("(") and tok.endswith(")")
    v = Decimal(tok.strip("()").replace(",", ""))
    return -v if neg else v


def _split_row(line: str) -> tuple[str, list[str]] | None:
    """(text without numbers and note refs, [current, prior]) or None if no value pair."""
    toks = line.split()
    runs: list[tuple[int, int]] = []
    i = 0
    while i < len(toks):
        if _NUM.match(toks[i]):
            j = i
            while j < len(toks) and _NUM.match(toks[j]):
                j += 1
            runs.append((i, j))
            i = j
        else:
            i += 1
    runs = [r for r in runs if r[1] - r[0] >= 2]
    if not runs:
        return None
    a, b = runs[-1]
    words = [t for k, t in enumerate(toks) if not (a <= k < b) and not _NOTE.match(t)]
    return " ".join(words), toks[b - 2 : b]


def _norm(text: str) -> str:
    t = text.lower().replace("\u2013", "-")  # en dash
    t = re.sub(r"[:]", " ", t)
    t = re.sub(r"\s+", " ", t).strip(" -")
    return re.sub(r"\s+", " ", t).strip()


def _units(text: str) -> tuple[str, int] | None:
    low = text.lower()
    m = re.search(r"\(disajikan dalam ([^)]*)\)", low)
    if not m:
        return None
    for pat, ccy, scale in _UNITS:
        if re.search(pat, m.group(1)):
            return ccy, scale
    return None


def _bs_dates(text: str) -> tuple[date, date] | None:
    m = re.search(r"(\d{1,2} [a-z]+ \d{4}) dan (\d{1,2} [a-z]+ \d{4})", text.lower())
    return (parse_date(m.group(1)), parse_date(m.group(2))) if m else None


def _pl_dates(text: str) -> tuple[date, date, int] | None:
    low = re.sub(r"\s+", " ", text.lower())
    m = re.search(r"berakhir (\d{1,2} [a-z]+ (\d{4})) dan (\d{4})", low)
    if not m:
        return None
    end = parse_date(m.group(1))
    prev = end.replace(year=int(m.group(3)))
    pm = re.search(r"(tiga|enam|sembilan) bulan", low)
    months = _PERIOD_MONTHS[pm.group(1)] if pm else (12 if "tahun yang berakhir" in low else 0)
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
    top = [ln.strip().upper() for ln in page.splitlines()[:25]]
    return any(ln.startswith(title) for ln in top) and "(DISAJIKAN DALAM" in page.upper()


def _shares_issued(bs_text: str) -> Decimal | None:
    """Issued ordinary shares from the share-capital caption.

    Indonesian figures use '.' thousands ("24.030.764.724 saham biasa"); the bilingual
    layout interleaves English words, so only "ditempatkan ... <n> saham biasa" is required.
    """
    flat = re.sub(r"\s+", " ", bs_text.lower())
    m = re.search(r"ditempatkan.{0,300}?(\d{1,3}(?:\.\d{3}){2,}) saham biasa", flat)
    return Decimal(m.group(1).replace(".", "")) if m else None


def parse_full_statements(pages: list[str]) -> ParsedPublication:
    errors: list[str] = []
    facts: list[ParsedFact] = []
    bs_pages = [p for p in pages if _is_statement_page(p, "LAPORAN POSISI KEUANGAN")]
    pl_pages = [p for p in pages if _is_statement_page(p, "LAPORAN LABA RUGI")]
    if not bs_pages or not pl_pages:
        out = ParsedPublication(date.min, None)
        out.errors.append("no balance sheet / income statement titles found")
        return out
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
                    _value(raw[col]) * unit_scale,
                    unit_scale,
                    statement,
                    label[:300],
                )
            )

    def walk(statement_pages: list[str], patterns: list[tuple[str, str]], statement: str) -> None:
        seen: set[str] = set()
        buf: list[str] = []
        attrib = False
        for page in statement_pages:
            for raw_line in page.splitlines():
                line = heal_numbers(raw_line.strip())
                if not line:
                    buf = []
                    continue
                split = _split_row(line)
                if split is None:
                    buf.append(_norm(line))
                    if statement == "income":
                        joined = " ".join(buf)
                        if "komprehensif" in joined or "per saham" in joined:
                            attrib = False
                        elif "laba" in joined and "diatribusikan" in joined:
                            attrib = True
                    continue
                text, raw = split
                line_text = _norm(text)
                # A label may wrap over the lines above it, but section headings
                # ("ASET LANCAR / CURRENT ASSETS") also sit there: try the shortest
                # candidate first and widen one line at a time.
                candidates = [_norm(" ".join([*buf[k:], line_text])) for k in range(len(buf), -1, -1)]
                row_text = candidates[-1]
                buf = []
                if not row_text:
                    continue
                concept = None
                for cand in candidates:
                    concept = next((name for name, pat in patterns if re.search(pat, cand)), None)
                    if concept is not None:
                        break
                if statement == "income":
                    if attrib and re.fullmatch(r"pemilik entitas induk owners of the parent", line_text):
                        concept = "net_income_parent"
                    elif attrib and re.fullmatch(
                        r"kepentingan non-?pengendali non-controlling interests?", line_text
                    ):
                        concept = "net_income_nci"
                    elif re.search(r"\blaba (bersih )?per saham\b", row_text):
                        concept = "eps_basic"
                if concept is None:
                    continue
                if concept in seen:
                    errors.append(f"{concept}: matched twice in the {statement} statement")
                    continue
                seen.add(concept)
                emit(concept, raw, statement, row_text)

    walk(bs_pages, _BS, "balance")
    walk(pl_pages, _PL, "income")

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
        ni = p.get("net_income", f.basis, f.period_end)
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
            if abs(pbt - tax - ni) > tol:
                problems.append(f"{end}: PBT {pbt} - tax {tax} != NI {ni}")
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
    parsed = parse_publication(pages)
    return parsed, ([] if parsed.errors else check_identities(parsed)), []
