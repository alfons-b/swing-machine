"""PDF -> text per page (pypdf, BSD licence).

Issuer PDFs are often AES-encrypted with an empty user password (only editing/printing is
restricted). pypdf opens them like any viewer does; `cryptography` provides AES. A file that
needs a real password is unreadable here and is reported, never cracked.
"""

from __future__ import annotations

import io

from pypdf import PdfReader
from pypdf.errors import DependencyError, FileNotDecryptedError, PdfReadError


def pdf_pages(content: bytes) -> list[str]:
    try:
        reader = PdfReader(io.BytesIO(content))
        return [p.extract_text() or "" for p in reader.pages]
    except (PdfReadError, FileNotDecryptedError, DependencyError, ValueError, KeyError) as exc:
        raise ValueError(f"unreadable PDF: {exc}") from exc
