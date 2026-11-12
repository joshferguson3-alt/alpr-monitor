"""PDF text extraction. pypdf first, pdfplumber if the text is thin."""

from __future__ import annotations

import io

from pypdf import PdfReader


def extract_pdf_text(data: bytes) -> str:
    primary = _pypdf_text(data)
    if len(primary.strip()) >= 40:
        return primary
    try:
        import pdfplumber
    except ImportError:
        return primary
    parts: list[str] = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for page in pdf.pages:
            parts.append(page.extract_text() or "")
    fallback = "\n".join(parts).strip()
    return fallback or primary


def _pypdf_text(data: bytes) -> str:
    reader = PdfReader(io.BytesIO(data))
    parts = []
    for page in reader.pages:
        parts.append(page.extract_text() or "")
    return "\n".join(parts).strip()
