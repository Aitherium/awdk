"""Tests for adk.docconvert — the document converter ladder behind 'adk ingest'."""

import logging
import sys
import types

import pytest

from adk import docconvert
from adk.ingest import FileWalker, TextChunker, ingest_files

CONTROL = "CONTROL-STRING-7731"


@pytest.fixture
def reset_hint(monkeypatch):
    """Each test starts with the one-per-process hint un-fired."""
    monkeypatch.setattr(docconvert, "_hinted", False)


# ─────────────────────────────────────────────────────────────────────────────
# FileWalker
# ─────────────────────────────────────────────────────────────────────────────

class TestWalkerDocuments:
    def test_excludes_pdf_without_converter_and_hints_once(
        self, tmp_path, monkeypatch, caplog, reset_hint
    ):
        monkeypatch.setattr(docconvert, "available_converters", lambda: [])
        (tmp_path / "a.pdf").write_bytes(b"%PDF-1.4")
        (tmp_path / "b.pdf").write_bytes(b"%PDF-1.4")
        (tmp_path / "notes.md").write_text("plain text")

        with caplog.at_level(logging.WARNING, logger="adk.docconvert"):
            files = FileWalker().walk(tmp_path)

        assert {f.name for f in files} == {"notes.md"}
        hints = [r for r in caplog.records if 'awdk[docs]' in r.getMessage()]
        assert len(hints) == 1

    def test_includes_pdf_with_converter(self, tmp_path, monkeypatch, reset_hint):
        monkeypatch.setattr(docconvert, "available_converters", lambda: ["pypdf"])
        (tmp_path / "a.pdf").write_bytes(b"%PDF-1.4")
        (tmp_path / "img.png").write_bytes(b"\x89PNG")

        files = FileWalker().walk(tmp_path)

        assert {f.name for f in files} == {"a.pdf"}

    def test_explicit_exclude_set_is_untouched(self, tmp_path, monkeypatch, reset_hint):
        monkeypatch.setattr(docconvert, "available_converters", lambda: ["pypdf"])
        (tmp_path / "a.pdf").write_bytes(b"%PDF-1.4")

        walker = FileWalker(exclude_extensions={"pdf"})

        assert walker.walk(tmp_path) == []


# ─────────────────────────────────────────────────────────────────────────────
# convert_document ladder
# ─────────────────────────────────────────────────────────────────────────────

def _install_fake_docling(monkeypatch, error):
    """Register a fake docling whose DocumentConverter.convert raises `error`."""
    root = types.ModuleType("docling")
    datamodel = types.ModuleType("docling.datamodel")
    base_models = types.ModuleType("docling.datamodel.base_models")
    pipeline_options = types.ModuleType("docling.datamodel.pipeline_options")
    document_converter = types.ModuleType("docling.document_converter")

    class InputFormat:
        PDF = "pdf"

    class NativePdfPipelineOptions:
        pass

    class PdfFormatOption:
        def __init__(self, pipeline_options=None):
            self.pipeline_options = pipeline_options

    class DocumentConverter:
        def __init__(self, format_options=None):
            self.format_options = format_options

        def convert(self, source):
            raise error

    base_models.InputFormat = InputFormat
    pipeline_options.NativePdfPipelineOptions = NativePdfPipelineOptions
    document_converter.PdfFormatOption = PdfFormatOption
    document_converter.DocumentConverter = DocumentConverter
    root.datamodel = datamodel
    datamodel.base_models = base_models
    datamodel.pipeline_options = pipeline_options
    root.document_converter = document_converter

    for name, mod in (
        ("docling", root),
        ("docling.datamodel", datamodel),
        ("docling.datamodel.base_models", base_models),
        ("docling.datamodel.pipeline_options", pipeline_options),
        ("docling.document_converter", document_converter),
    ):
        monkeypatch.setitem(sys.modules, name, mod)


def _install_fake_pypdf(monkeypatch, pages):
    """Register a fake pypdf whose PdfReader yields pages with the given text."""
    mod = types.ModuleType("pypdf")

    class _Page:
        def __init__(self, text):
            self._text = text

        def extract_text(self):
            return self._text

    class PdfReader:
        def __init__(self, source):
            self.pages = [_Page(t) for t in pages]

    mod.PdfReader = PdfReader
    monkeypatch.setitem(sys.modules, "pypdf", mod)


class TestConvertDocument:
    def test_falls_from_raising_docling_to_pypdf(self, tmp_path, monkeypatch, caplog):
        _install_fake_docling(monkeypatch, RuntimeError("docling exploded"))
        _install_fake_pypdf(monkeypatch, ["page one", "page two"])
        pdf = tmp_path / "doc.pdf"
        pdf.write_bytes(b"%PDF-1.4")

        assert docconvert.available_converters() == ["docling", "pypdf"]
        with caplog.at_level(logging.WARNING, logger="adk.docconvert"):
            text = docconvert.convert_document(pdf)

        assert text == "page one\n\npage two"
        assert any("docling exploded" in r.getMessage() for r in caplog.records)

    def test_pypdf_is_pdf_only(self, tmp_path, monkeypatch):
        monkeypatch.delitem(sys.modules, "docling", raising=False)
        monkeypatch.setattr(docconvert, "_CONVERTER_ORDER", ("pypdf",))
        _install_fake_pypdf(monkeypatch, ["should not be read"])
        docx = tmp_path / "doc.docx"
        docx.write_bytes(b"PK")

        assert docconvert.convert_document(docx) is None

    def test_returns_none_without_converters(self, tmp_path, monkeypatch):
        monkeypatch.setattr(docconvert, "available_converters", lambda: [])
        pdf = tmp_path / "doc.pdf"
        pdf.write_bytes(b"%PDF-1.4")

        assert docconvert.convert_document(pdf) is None

    def test_non_document_extension_is_none(self, tmp_path):
        assert docconvert.convert_document(tmp_path / "readme.txt") is None


# ─────────────────────────────────────────────────────────────────────────────
# ingest_files end to end (dry run)
# ─────────────────────────────────────────────────────────────────────────────

class TestIngestDocuments:
    async def test_dry_run_chunks_converted_pdf(self, tmp_path, monkeypatch, reset_hint):
        monkeypatch.setattr(docconvert, "available_converters", lambda: ["pypdf"])
        monkeypatch.setattr(docconvert, "convert_document", lambda p: CONTROL)
        seen = []
        (tmp_path / "doc.pdf").write_bytes(b"%PDF-1.4")

        original = TextChunker.chunk

        def _spy(self, text, source=""):
            chunks = original(self, text, source)
            seen.extend(c["text"] for c in chunks)
            return chunks

        monkeypatch.setattr(TextChunker, "chunk", _spy)

        result = await ingest_files(tmp_path, dry_run=True)

        assert result.files_ingested == 1
        assert result.chunks_created == 1
        assert len(seen) == 1 and CONTROL in seen[0]

    async def test_no_converter_result_counts_as_skipped(
        self, tmp_path, monkeypatch, reset_hint
    ):
        monkeypatch.setattr(docconvert, "available_converters", lambda: ["pypdf"])
        monkeypatch.setattr(docconvert, "convert_document", lambda p: None)
        (tmp_path / "doc.pdf").write_bytes(b"%PDF-1.4")

        result = await ingest_files(tmp_path, dry_run=True)

        assert result.files_ingested == 0
        assert result.files_skipped == 1
        assert result.skipped_files == [(str((tmp_path / "doc.pdf").resolve()),
                                         "no document converter")]

    async def test_secret_in_converted_content_is_skipped(
        self, tmp_path, monkeypatch, reset_hint
    ):
        monkeypatch.setattr(docconvert, "available_converters", lambda: ["pypdf"])
        fake_key = "ghp_" + "A" * 40
        monkeypatch.setattr(docconvert, "convert_document", lambda p: f"token {fake_key}")
        (tmp_path / "doc.pdf").write_bytes(b"%PDF-1.4")

        result = await ingest_files(tmp_path, dry_run=True)

        assert result.files_ingested == 0
        assert result.files_skipped == 1
        assert "secret" in result.skipped_files[0][1]
