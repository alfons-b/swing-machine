"""Parser for the OJK-standard bank publication report ("Laporan Publikasi").

All Indonesian banks publish quarterly statements in this prescribed layout:
numbered line items, four columns (individual current/prior, consolidated
current/prior), amounts "dalam jutaan Rupiah", Indonesian number format.
The parser works on extracted *text*, so it can be tested without any PDF.

It maps only lines it is sure about. Anything ambiguous — a mapped label seen
twice, a truncated row, a missing column header — is an error, never a guess.
Accounting identities are checked before anything is stored (see `check_identities`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from idx_trading.fundamentals.concepts import CONCEPTS, Kind

MILLION = 1_000_000
_MONTHS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "mei": 5,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "agu": 8,
    "ags": 8,
    "aug": 8,
    "sep": 9,
    "okt": 10,
    "oct": 10,
    "nov": 11,
    "des": 12,
    "dec": 12,
}
_DATE = re.compile(r"\b(\d{1,2})\s+([A-Za-z]{3})[A-Za-z]*\s+(\d{4})\b")
_NUM = r"\(?-?\d{1,3}(?:\.\d{3})*\)?|-"
_ROW = re.compile(rf"^(?P<label>.*?\S)\s+(?P<nums>(?:(?:{_NUM})\s+)*(?:{_NUM}))\s*$")
_ENUM = re.compile(r"^\s*(?:\d{1,2}\.|[a-z]\.|[ivx]+\.)\s*", re.I)


def _norm(label: str) -> str:
    s = _ENUM.sub("", label).lower()
    s = re.sub(r"\s+", " ", s).strip()
    # Interim reports say "periode berjalan" where annual ones say "tahun berjalan".
    return s.replace("periode berjalan", "tahun berjalan")


# normalized label -> (concept, statement, consolidated_only, scale)
_BS: dict[str, str] = {
    "total aset": "total_assets",
    "kredit dan pembiayaan yang diberikan": "loans",
    "giro": "deposits_current",
    "tabungan": "deposits_savings",
    "deposito": "deposits_time",
    "total liabilitas": "total_liabilities",
    "total ekuitas": "total_equity",
    "total liabilitas dan ekuitas": "total_liabilities_and_equity",
}
_PL: dict[str, str] = {
    "pendapatan (beban) bunga bersih": "net_interest_income",
    "laba (rugi) operasional": "operating_profit",
    "laba (rugi) tahun berjalan sebelum pajak": "profit_before_tax",
    "pajak penghasilan": "income_tax",
    "laba (rugi) bersih tahun berjalan": "net_income",
    "laba bersih per saham (dalam satuan rupiah)": "eps_basic",
}
_ATTRIB_HEADER = "laba (rugi) bersih tahun berjalan yang dapat diatribusikan"
_ATTRIB_END = ("total laba (rugi) bersih tahun berjalan",)
_CONSOLIDATED_ONLY = {"net_income_nci", "eps_basic"}
_UNIT_SCALE = {"eps_basic": 1}


def parse_number(tok: str) -> Decimal:
    tok = tok.strip()
    if tok == "-":
        return Decimal(0)
    neg = tok.startswith("(") and tok.endswith(")")
    core = tok.strip("()").replace(".", "")
    if not re.fullmatch(r"-?\d+", core):
        raise ValueError(f"not a number: {tok!r}")
    v = Decimal(core)
    return -v if neg else v


def parse_date(text: str) -> date:
    m = _DATE.search(text)
    if not m:
        raise ValueError(f"no date in {text!r}")
    month = _MONTHS.get(m.group(2).lower())
    if month is None:
        raise ValueError(f"unknown month {m.group(2)!r}")
    return date(int(m.group(3)), month, int(m.group(1)))


@dataclass(frozen=True)
class ParsedFact:
    concept: str
    basis: str  # consolidated | individual
    period_start: date | None
    period_end: date
    value: Decimal  # already scaled to the unit (IDR or IDR/share)
    scale: int
    statement: str
    source_label: str


@dataclass
class ParsedPublication:
    period_end: date
    audited: bool | None
    facts: list[ParsedFact] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    currency: str = "IDR"
    document_type: str = "bank_publication_quarterly"

    def get(self, concept: str, basis: str, period_end: date) -> Decimal | None:
        for f in self.facts:
            if f.concept == concept and f.basis == basis and f.period_end == period_end:
                return f.value
        return None


def _header(line: str) -> list[date] | None:
    dates = [parse_date(m.group(0)) for m in _DATE.finditer(line)]
    return dates if len(dates) == 4 and not _ROW.match(_DATE.sub("", line).strip() or "x") else None


def _audit_flags(line: str) -> list[bool]:
    return [m.lower() == "diaudit" for m in re.findall(r"(?i)tidak diaudit|diaudit", line)]


def parse_publication(pages: list[str]) -> ParsedPublication:
    """`pages`: extracted text per page, in order."""
    out: ParsedPublication | None = None
    errors: list[str] = []
    seen: set[tuple[str, str]] = set()
    statement: str | None = None
    headers: dict[str, list[date]] = {}
    audit: dict[str, list[bool]] = {}
    in_attrib = False
    done = False

    for page in pages:
        if done:
            break
        lines = page.splitlines()
        page_upper = page.upper()
        page_stmt = (
            "income"
            if "PENDAPATAN DAN BEBAN OPERASIONAL" in page_upper
            else "balance"
            if ("ASET" in page_upper or "LIABILITAS" in page_upper) and statement in (None, "balance")
            else None
        )
        if page_stmt is None:
            continue
        statement = page_stmt
        for i, raw in enumerate(lines):
            line = raw.strip()
            if not line:
                continue
            hdr = _header(line)
            if hdr is not None:
                headers.setdefault(statement, hdr)
                flags = _audit_flags(lines[i - 1]) if i > 0 else []
                if len(flags) == 4:
                    audit.setdefault(statement, flags)
                continue
            m = _ROW.match(line)
            label = _norm(m.group("label") if m else line)
            if statement == "income":
                if label.startswith(_ATTRIB_HEADER):
                    in_attrib = True
                    continue
                if any(label.startswith(e) for e in _ATTRIB_END):
                    in_attrib = False
            if m is None:
                continue
            concept = None
            if statement == "balance":
                concept = _BS.get(label)
            else:
                concept = _PL.get(label)
                if in_attrib and label == "pemilik":
                    concept = "net_income_parent"
                elif in_attrib and label == "kepentingan non-pengendali":
                    concept = "net_income_nci"
            if concept is None:
                continue
            key = (statement, concept)
            if key in seen:
                errors.append(f"{concept}: label {label!r} appears twice in the {statement} statement")
                continue
            seen.add(key)
            hdr_dates = headers.get(statement)
            if hdr_dates is None:
                errors.append(f"{concept}: no column header before this line")
                continue
            try:
                nums = [parse_number(t) for t in m.group("nums").split()]
            except ValueError as exc:
                errors.append(f"{concept}: {exc}")
                continue
            scale = _UNIT_SCALE.get(concept, MILLION)
            if len(nums) == 4:
                cols = [("individual", 0), ("individual", 1), ("consolidated", 2), ("consolidated", 3)]
                values = list(zip(cols, nums, strict=True))
            elif len(nums) == 2 and concept in _CONSOLIDATED_ONLY:
                values = [(("consolidated", 2), nums[0]), (("consolidated", 3), nums[1])]
            else:
                errors.append(f"{concept}: expected 4 columns, found {len(nums)} (truncated row?)")
                continue
            for (basis, col), v in values:
                end = hdr_dates[col]
                start = date(end.year, 1, 1) if CONCEPTS[concept].kind == Kind.DURATION else None
                (out := out or ParsedPublication(hdr_dates[0], None)).facts.append(
                    ParsedFact(concept, basis, start, end, v * scale, scale, statement, m.group("label"))
                )
            if concept == "eps_basic":
                done = True
                break

    if out is None:
        out = ParsedPublication(date.min, None)
        errors.append("no recognisable bank publication statements found")
    bs, pl = headers.get("balance"), headers.get("income")
    if bs and pl and bs[0] != pl[0]:
        errors.append(f"balance sheet date {bs[0]} differs from income statement date {pl[0]}")
    if bs:
        out.period_end = bs[0]
    audit_cols = audit.get("balance") or audit.get("income")
    out.audited = audit_cols[0] if audit_cols else None
    out.errors.extend(errors)
    return out


REQUIRED = (
    "total_assets",
    "total_liabilities",
    "total_equity",
    "total_liabilities_and_equity",
    "profit_before_tax",
    "income_tax",
    "net_income",
    "net_income_parent",
)


def check_identities(p: ParsedPublication, tolerance: Decimal = Decimal(2 * MILLION)) -> list[str]:
    """Accounting identities that must hold in the printed numbers (rounded to Rp 1 million)."""
    problems: list[str] = []
    present = {f.concept for f in p.facts}
    for c in REQUIRED:
        if c not in present:
            problems.append(f"missing required concept {c}")
    if problems:
        return problems
    periods = sorted({(f.basis, f.period_end, f.statement) for f in p.facts})

    def g(c: str, basis: str, end: date) -> Decimal | None:
        return p.get(c, basis, end)

    for basis, end, stmt in periods:
        if stmt == "balance":
            ta, tl, te, tle = (
                g(c, basis, end)
                for c in ("total_assets", "total_liabilities", "total_equity", "total_liabilities_and_equity")
            )
            if None in (ta, tl, te, tle):
                problems.append(f"{basis} {end}: balance sheet totals incomplete")
                continue
            assert ta is not None and tl is not None and te is not None and tle is not None
            if abs(ta - tle) > tolerance:
                problems.append(f"{basis} {end}: total assets {ta} != liabilities+equity {tle}")
            if abs(tl + te - tle) > tolerance:
                problems.append(f"{basis} {end}: liabilities {tl} + equity {te} != {tle}")
        else:
            pbt, tax, ni, nip = (
                g(c, basis, end)
                for c in ("profit_before_tax", "income_tax", "net_income", "net_income_parent")
            )
            if None in (pbt, tax, ni, nip):
                continue  # consolidated-only rows (EPS/NCI) create partial periods
            assert pbt is not None and tax is not None and ni is not None and nip is not None
            if abs(pbt - tax - ni) > tolerance:
                problems.append(f"{basis} {end}: profit before tax {pbt} - tax {tax} != net income {ni}")
            nci = g("net_income_nci", basis, end) or Decimal(0)
            if abs(nip + nci - ni) > tolerance:
                problems.append(f"{basis} {end}: owners {nip} + NCI {nci} != net income {ni}")
    return problems
