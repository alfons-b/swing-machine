"""Financial-statement adapters.

* `BcaIrAdapter` (registry `bca_ir`): discovers quarterly OJK publication reports
  on BCA's investor-relations page (structured `mappedReports` list embedded in the
  page) and downloads the PDFs not fetched before. Publication date = the YYYYMMDD
  prefix of the file name; a second date prefix marks a re-upload (revision).
* `ManualPublicationAdapter` (registry `manual_csv`): a bank publication PDF the
  owner saved by hand, with the ticker and publication date stated explicitly.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from idx_trading.db.enums import PermissionPurpose
from idx_trading.errors import SourceHttpError
from idx_trading.ingestion.adapters.base import SourceAdapter
from idx_trading.ingestion.http import HttpClient
from idx_trading.ingestion.schemas import FetchRequest, ParseResult, RawPayload

DOCUMENT_TYPE = "bank_publication_quarterly"
BCA_IR_URL = "https://www.bca.co.id/id/tentang-bca/hubungan-investor/laporan-presentasi/laporan-keuangan"
_QUARTERLY_TITLE = re.compile(
    r"^Laporan Keuangan (Maret|Mar|Juni|Jun|September|Sep|Desember|Des) (\d{4})$", re.I
)
_FILE_DATES = re.compile(r"/(\d{8})(?:_(\d{8}))?-[^/]*\.pdf$", re.I)


@dataclass(frozen=True)
class ReportLink:
    title: str
    url: str
    published_on: date  # date this file was published (latest date prefix)
    original_published_on: date  # first publication of the content (earliest prefix)


def _ymd(s: str) -> date:
    return date(int(s[:4]), int(s[4:6]), int(s[6:8]))


def parse_bca_index(html: str) -> list[ReportLink]:
    """Quarterly publication reports listed on the BCA IR page (explicit pattern, no guessing)."""
    text = html.replace('\\"', '"').replace("\\\\", "\\")
    items = re.findall(
        r'"title":"([^"]+)","description":"[^"]*","size":"[^"]*",'
        r'"sizeBytes":\d+,"year":\d+,"url":"([^"]+)"',
        text,
    )
    out: dict[str, ReportLink] = {}
    for title, url in items:
        if not _QUARTERLY_TITLE.match(title.strip()):
            continue
        m = _FILE_DATES.search(url)
        if not m:
            continue  # no publication date in the file name -> not usable point-in-time
        dates = sorted(_ymd(d) for d in m.groups() if d)
        out[url] = ReportLink(title.strip(), url, dates[-1], dates[0])
    return sorted(out.values(), key=lambda r: r.published_on)


class FinancialReportAdapter(SourceAdapter):
    dataset = "financial_reports"

    def parse(self, payload: RawPayload) -> ParseResult:  # pragma: no cover - contract guard
        raise TypeError("financial-report payloads are parsed by FinancialReportPipeline")


class BcaIrAdapter(FinancialReportAdapter):
    source_id = "bca_ir"
    purpose = PermissionPurpose.AUTOMATED_INGESTION
    TICKER = "BBCA"

    def __init__(self, client: HttpClient, index_url: str = BCA_IR_URL) -> None:
        self.client = client
        self.index_url = index_url

    def fetch(self, request: FetchRequest) -> Iterable[RawPayload]:
        return self._iter(request)

    def _iter(self, request: FetchRequest) -> Iterator[RawPayload]:
        skip: set[str] = set(request.options.get("skip_urls") or [])
        limit = int(request.options.get("limit", 20))
        idx = self.client.get(self.index_url)
        if idx.status != 200:
            raise SourceHttpError(idx.status, idx.url, "BCA IR index unavailable")
        links = parse_bca_index(idx.body.decode("utf-8", "replace"))
        yield RawPayload(
            idx.body,
            "text/html",
            idx.url,
            datetime.now(tz=UTC),
            {"kind": "ir_index", "ticker": self.TICKER, "links": len(links)},
        )
        if request.start_date:
            links = [lk for lk in links if lk.published_on >= request.start_date]
        for link in [lk for lk in links if lk.url not in skip][:limit]:
            resp = self.client.get(link.url)
            if resp.status != 200:
                raise SourceHttpError(resp.status, resp.url, f"report {link.title!r}")
            if not resp.body.startswith(b"%PDF"):
                raise SourceHttpError(resp.status, resp.url, "response is not a PDF")
            yield RawPayload(
                resp.body,
                "application/pdf",
                link.url,
                datetime.now(tz=UTC),
                {
                    "kind": DOCUMENT_TYPE,
                    "ticker": self.TICKER,
                    "title": link.title,
                    "url": link.url,
                    "published_on": link.published_on.isoformat(),
                    "original_published_on": link.original_published_on.isoformat(),
                    "published_at_basis": "filename_date",
                },
            )


class ManualPublicationAdapter(FinancialReportAdapter):
    source_id = "manual_csv"
    purpose = PermissionPurpose.MANUAL_IMPORT

    def fetch(self, request: FetchRequest) -> Iterable[RawPayload]:
        ticker = request.options.get("ticker")
        published = request.options.get("published_on")
        if not ticker or not published or len(request.paths) != 1:
            raise ValueError("needs exactly one path, a ticker and published_on (YYYY-MM-DD)")
        date.fromisoformat(str(published))
        path = Path(request.paths[0])
        content = path.read_bytes()
        if not content.startswith(b"%PDF"):
            raise ValueError(f"{path.name} is not a PDF")
        basis = str(request.options.get("published_at_basis") or "stated_by_owner")
        # received_by_owner: no publication date could be established, so the date the owner
        # handed the file over is used. Later than the real one, never earlier: no look-ahead.
        if basis not in {"stated_by_owner", "filename_date", "press_report_date", "received_by_owner"}:
            raise ValueError(f"unknown published_at_basis {basis!r}")
        return [
            RawPayload(
                content,
                "application/pdf",
                str(request.options.get("origin_url") or path.name),
                datetime.now(tz=UTC),
                {
                    "kind": "financial_report",
                    "ticker": str(ticker).upper(),
                    "title": str(request.options.get("title") or path.name),
                    "file_name": path.name,
                    "published_on": str(published),
                    "original_published_on": str(published),
                    "published_at_basis": basis,
                },
            )
        ]


__all__ = ["BcaIrAdapter", "ManualPublicationAdapter", "ReportLink", "parse_bca_index"]
