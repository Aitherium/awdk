"""PDFReportGenerator — Compliance report PDF export for AitherShell.

Self-contained port of AitherOS lib/compliance/PDFReportGenerator.py.
Uses fpdf2 (pure Python, MIT, zero native deps).

Usage:
    from adk.compliance.pdf_report import generate_attestation_pdf

    pdf_bytes = generate_attestation_pdf(report.to_dict())
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List

logger = logging.getLogger("adk.compliance.pdf_report")

try:
    from fpdf import FPDF
    FPDF_AVAILABLE = True
except ImportError:
    FPDF_AVAILABLE = False
    FPDF = None


class _CompliancePDF(FPDF if FPDF_AVAILABLE else object):
    """Base PDF with AitherOS compliance header/footer."""

    def __init__(self):
        if not FPDF_AVAILABLE:
            raise ImportError("fpdf2 is required for PDF generation: pip install fpdf2")
        super().__init__()
        self.set_auto_page_break(auto=True, margin=20)

    def header(self):
        self.set_font("Helvetica", "B", 14)
        self.cell(0, 10, "AitherOS Compliance Report", align="C", new_x="LMARGIN", new_y="NEXT")
        self.set_font("Helvetica", "", 8)
        self.cell(
            0, 5,
            f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}",
            align="C", new_x="LMARGIN", new_y="NEXT",
        )
        self.ln(5)

    def footer(self):
        self.set_y(-15)
        self.set_font("Helvetica", "I", 8)
        self.cell(0, 10, f"Page {self.page_no()}/{{nb}}", align="C")

    def section_title(self, title: str):
        self.set_font("Helvetica", "B", 12)
        self.set_fill_color(230, 230, 240)
        self.cell(0, 8, f"  {title}", fill=True, new_x="LMARGIN", new_y="NEXT")
        self.ln(3)

    def kv_row(self, key: str, value: str):
        self.set_font("Helvetica", "B", 9)
        self.cell(55, 6, key)
        self.set_font("Helvetica", "", 9)
        text = _latin1(value)
        if self.get_string_width(text) > self.w - self.r_margin - self.get_x():
            # A signature or public key is wider than the page: wrap it instead of
            # drawing past the margin, where it is clipped and cannot be re-typed.
            self.multi_cell(0, 5, text, new_x="LMARGIN", new_y="NEXT")
        else:
            self.cell(0, 6, text, new_x="LMARGIN", new_y="NEXT")

    def table_header(self, cols: List[str], widths: List[int]):
        self.set_font("Helvetica", "B", 8)
        self.set_fill_color(200, 200, 210)
        for col, w in zip(cols, widths):
            self.cell(w, 6, col, border=1, fill=True)
        self.ln()

    def table_row(self, values: List[str], widths: List[int]):
        self.set_font("Helvetica", "", 8)
        for val, w in zip(values, widths):
            self.cell(w, 5, _latin1(val)[:50], border=1)
        self.ln()


def _latin1(value: Any) -> str:
    """The core PDF fonts are latin-1: replace what they cannot draw, never raise."""
    return str(value).encode("latin-1", "replace").decode("latin-1")


def _model_section(pdf: "_CompliancePDF", model: Dict[str, Any],
                   license_info: Any) -> None:
    pdf.section_title("Model and Data Boundary")
    pdf.kv_row("Boundary:", str(model.get("boundary", "unknown")).upper())
    pdf.kv_row("Why:", model.get("boundary_reason", ""))
    pdf.kv_row("Provider:", f"{model.get('provider', '')} ({model.get('mode', '')})")
    pdf.kv_row("Model:", model.get("model", "") or "(server default)")
    pdf.kv_row("Endpoint:", model.get("base_url", "") or "(provider default)")
    if isinstance(license_info, dict):
        pdf.kv_row("License:", license_info.get("license", "unknown"))
        pdf.kv_row("Commercial use:", "Yes" if license_info.get("commercial_ok") else "No")
    else:
        pdf.kv_row("License:", "not in the bundled model license registry")
    pdf.ln(3)


def _receipts_section(pdf: "_CompliancePDF", rcpt: Dict[str, Any]) -> None:
    verify = rcpt.get("verify", {})
    pdf.section_title("Signed Receipts (what the agent did)")
    pdf.kv_row("Verify verdict:", f"{str(verify.get('verdict', '')).upper()} "
                                  f"(exit {verify.get('code', '?')})")
    pdf.kv_row("Reason:", verify.get("reason", ""))
    pdf.kv_row("Rows (all / window):",
               f"{rcpt.get('rows_total', 0)} / {rcpt.get('rows_in_window', 0)}")
    pdf.kv_row("Refused in window:", str(rcpt.get("refused_in_window", 0)))
    pdf.kv_row("Unsigned in window:", str(rcpt.get("unsigned_in_window", 0)))
    kinds = rcpt.get("by_kind") or {}
    if kinds:
        pdf.kv_row("By kind:", ", ".join(f"{k} {v}" for k, v in sorted(kinds.items())))
    tools = rcpt.get("tools") or {}
    if tools:
        widths = [120, 50]
        pdf.table_header(["Tool", "Calls in window"], widths)
        for name, count in tools.items():
            pdf.table_row([name, str(count)], widths)
    pdf.ln(3)


def generate_attestation_pdf(report: Dict[str, Any]) -> bytes:
    """Generate a PDF from an AttestationReport dict."""
    if not FPDF_AVAILABLE:
        raise ImportError("fpdf2 is required for PDF generation: pip install fpdf2")

    pdf = _CompliancePDF()
    pdf.alias_nb_pages()
    pdf.add_page()

    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(0, 12, "Data Boundary Attestation Report", align="C",
             new_x="LMARGIN", new_y="NEXT")
    pdf.ln(5)

    # Report metadata
    pdf.section_title("Report Information")
    pdf.kv_row("Report ID:", report.get("report_id", ""))
    pdf.kv_row("Generated:", report.get("generated_at", ""))
    pdf.kv_row("Window Start:", report.get("window_start", ""))
    pdf.kv_row("Window End:", report.get("window_end", ""))
    pdf.kv_row("Node ID:", report.get("node_id", ""))
    if report.get("agent"):
        pdf.kv_row("Agent:", report["agent"])
    pdf.ln(3)

    # Air-gap status
    air_gap = report.get("air_gap", {})
    pdf.section_title("Air-Gap Enforcement Status")
    enforced = air_gap.get("enforced", False)
    pdf.kv_row("Enforced:", "YES" if enforced else "NO")
    pdf.kv_row("Mode:", air_gap.get("mode", "disabled"))
    pdf.kv_row("Activated At:", air_gap.get("activated_at", "N/A") or "N/A")
    if enforced:
        pdf.set_font("Helvetica", "B", 10)
        pdf.set_text_color(0, 128, 0)
        pdf.cell(0, 8, "DATA LOCALITY ENFORCED -- No cloud egress permitted",
                 new_x="LMARGIN", new_y="NEXT")
        pdf.set_text_color(0, 0, 0)
    pdf.ln(3)

    # Model boundary (Agent Home reports; absent from agent attestations)
    if isinstance(report.get("model"), dict):
        _model_section(pdf, report["model"], report.get("model_license"))

    # LLM call summary -- only when a call log was counted. A report built without
    # one must not print "Total Calls: 0", which reads as a measured zero.
    llm = report.get("llm_summary")
    if isinstance(llm, dict):
        pdf.section_title("LLM Inference Summary")
        pdf.kv_row("Total Calls:", str(llm.get("total_calls", 0)))
        pdf.kv_row("Local vLLM:", str(llm.get("local_vllm_calls", 0)))
        pdf.kv_row("Local Ollama:", str(llm.get("local_ollama_calls", 0)))
        pdf.kv_row("Cloud Calls:", str(llm.get("cloud_calls", 0)))
        pdf.kv_row("Failed Calls:", str(llm.get("failed_calls", 0)))
        models = llm.get("models_used", [])
        if models:
            pdf.kv_row("Models Used:", ", ".join(models))
        pdf.ln(3)

    if isinstance(report.get("receipts"), dict):
        _receipts_section(pdf, report["receipts"])

    # Violations
    violations = report.get("violations", [])
    if "egress_events_total" in report:
        total = int(report.get("egress_events_total") or 0)
        shown = f"; first {len(violations)} listed" if total > len(violations) else ""
        pdf.section_title(f"Egress Outside Policy ({total}{shown})")
    else:
        pdf.section_title(f"Air-Gap Violations ({len(violations)})")
    if violations:
        widths = [35, 40, 65, 30]
        pdf.table_header(["Timestamp", "Subsystem", "Detail", "Action"], widths)
        for v in violations[:50]:
            pdf.table_row([
                v.get("timestamp", "")[:19],
                v.get("subsystem", ""),
                v.get("detail", ""),
                v.get("action_taken", ""),
            ], widths)
    else:
        pdf.set_font("Helvetica", "I", 9)
        pdf.cell(0, 6, "No violations recorded in this window.",
                 new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)

    # Integrity block
    integrity = report.get("integrity", {})
    pdf.section_title("Report Integrity")
    pdf.kv_row("Content Hash:", integrity.get("content_hash", ""))
    pdf.kv_row("Signature:", integrity.get("signature", ""))
    algorithm = integrity.get("algorithm", "HMAC-SHA256")
    pdf.kv_row("Algorithm:", algorithm)
    if integrity.get("key_id"):
        pdf.kv_row("Key ID:", integrity.get("key_id", ""))
        pdf.kv_row("Public Key:", integrity.get("public_key", ""))
    if integrity.get("signed") is False:
        pdf.kv_row("Signed:", "NO -- no device key was available")
    pdf.ln(5)

    pdf.set_font("Helvetica", "", 9)
    pdf.cell(0, 6, "This report was automatically generated by AitherOS / AitherShell.",
             new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 6, f"The {algorithm} signature provides tamper evidence.",
             new_x="LMARGIN", new_y="NEXT")
    pdf.ln(10)
    pdf.line(20, pdf.get_y(), 100, pdf.get_y())
    pdf.ln(2)
    pdf.cell(0, 6, "Authorized Signature / Date", new_x="LMARGIN", new_y="NEXT")

    return bytes(pdf.output())


def generate_license_report_pdf(license_data: Dict[str, Any]) -> bytes:
    """Generate a PDF of model license information."""
    if not FPDF_AVAILABLE:
        raise ImportError("fpdf2 is required for PDF generation: pip install fpdf2")

    pdf = _CompliancePDF()
    pdf.alias_nb_pages()
    pdf.add_page()

    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(0, 12, "Model License Compliance Report", align="C",
             new_x="LMARGIN", new_y="NEXT")
    pdf.ln(5)

    pdf.section_title("Summary")
    pdf.kv_row("Total Models:", str(license_data.get("total_models", 0)))
    pdf.kv_row("Commercial OK:", str(license_data.get("commercial_ok", 0)))
    pdf.kv_row("Restricted:", str(license_data.get("restricted", 0)))
    pdf.kv_row("Attribution Required:", str(license_data.get("attribution_required", 0)))
    pdf.ln(3)

    pdf.section_title("Model Details")
    widths = [40, 30, 25, 25, 50]
    pdf.table_header(["Model", "License", "Commercial", "Attribution", "Provenance"], widths)

    models = license_data.get("models", {})
    for model_id, info in models.items():
        pdf.table_row([
            info.get("display_name", model_id),
            info.get("license", "unknown"),
            "Yes" if info.get("commercial_ok") else "No",
            "Yes" if info.get("attribution_required") else "No",
            info.get("provenance", ""),
        ], widths)

    return bytes(pdf.output())
