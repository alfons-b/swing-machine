"""PDF -> text per page (pypdf, pure Python, BSD licence)."""

from __future__ import annotations

import io

from pypdf import PdfReader
from pypdf.errors import PdfReadError


def pdf_pages(content: bytes) -> list[str]:
    try:
        reader = PdfReader(io.BytesIO(content))
        return [p.extract_text() or "" for p in reader.pages]
    except (PdfReadError, ValueError, KeyError) as exc:
        raise ValueError(f"unreadable PDF: {exc}") from exc
