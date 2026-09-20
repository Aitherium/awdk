"""Document-to-text converter ladder for 'adk ingest' (PDF/DOCX/PPTX/XLSX).

Docling (MIT, docling-project/docling) is taken in as an OPTIONAL dependency
(``pip install "awdk[docs]"``), never vendored. Every converter import is lazy
and guarded, so a bare ``awdk`` install keeps working and simply skips these files.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger("adk.docconvert")

__all__ = [
    "DOCUMENT_EXTENSIONS",
    "available_converters",
    "convert_document",
    "hint_once",
]

DOCUMENT_EXTENSIONS = frozenset({"pdf", "docx", "pptx", "xlsx"})

_CONVERTER_ORDER = ("docling", "pypdf")

_hinted = False


def available_converters() -> List[str]:
    """Return the converter names that import successfully, in ladder order.

    Returns:
        Subset of ``["docling", "pypdf"]``; empty when no converter is installed.
    """
    found: List[str] = []
    for name in _CONVERTER_ORDER:
        try:
            __import__(name)
        except ImportError:
            continue
        found.append(name)
    return found


def _convert_with_docling(path: Path) -> str:
    """Convert via docling's model-free native PDF pipeline (no torch needed)."""
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import NativePdfPipelineOptions
    from docling.document_converter import DocumentConverter, PdfFormatOption

    conv = DocumentConverter(
        format_options={
            InputFormat.PDF: PdfFormatOption(pipeline_options=NativePdfPipelineOptions()),
        }
    )
    result = conv.convert(str(path))
    return result.document.export_to_markdown()


def _convert_with_pypdf(path: Path) -> str:
    """Convert a PDF by joining pypdf's per-page text extraction."""
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    return "\n\n".join((page.extract_text() or "") for page in reader.pages)


def convert_document(path: Path) -> Optional[str]:
    """Convert a document file to text, trying each converter in turn.

    Ladder: docling (all formats) -> pypdf (``.pdf`` only) -> ``None``.
    A converter that raises is logged at warning and the next rung is tried.

    Args:
        path: The document to convert.

    Returns:
        Extracted text, or ``None`` when no converter produced any.
    """
    path = Path(path)
    ext = path.suffix.lstrip(".").lower()
    if ext not in DOCUMENT_EXTENSIONS:
        return None

    for name in available_converters():
        if name == "pypdf" and ext != "pdf":
            continue
        try:
            if name == "docling":
                return _convert_with_docling(path)
            if name == "pypdf":
                return _convert_with_pypdf(path)
        except Exception as exc:
            logger.warning("%s failed on %s: %s", name, path, exc)
    return None


def hint_once() -> None:
    """Log the install hint for document conversion once per process."""
    global _hinted
    if _hinted:
        return
    _hinted = True
    logger.warning(
        'document files skipped: install "awdk[docs]" for PDF/DOCX/PPTX/XLSX conversion'
    )
