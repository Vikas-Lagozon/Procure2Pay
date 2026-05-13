# quotation_agent/utils.py
"""
Reusable utility helpers for the Quotation Agent.

Covers:
  - File path detection & extraction
  - Text extraction (.docx / .pdf / .txt / .md)
  - Safe JSON parsing
  - Command parsing
  - Quotation record formatting
  - Document context building for Q&A
  - Session-state document loading
"""

from __future__ import annotations

import sys
from pathlib import Path

# ── Ensure both the agent dir and root dir are on sys.path ──────────────────
_AGENT_DIR = Path(__file__).resolve().parent   # quotation_agent/
_ROOT_DIR  = _AGENT_DIR.parent                 # project root
for _p in (_AGENT_DIR, _ROOT_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
# ─────────────────────────────────────────────────────────────────────────────

import json
import re

from docx import Document
import pdfplumber

from logger import get_logger

logger = get_logger(__name__)

_SUPPORTED_EXTS: frozenset[str] = frozenset({".docx", ".pdf", ".txt", ".md"})

# Matches absolute Windows, absolute Unix, AND relative paths
_FILE_RE = re.compile(
    r'(?:[A-Za-z]:[\\\/][\S]+|\/[\S]+|[\w][\w\/\\.\-]*)\.(?:docx|pdf|txt|md)',
    re.IGNORECASE,
)


# ─────────────────────────────────────────────────────────────
# FILE PATH HELPERS
# ─────────────────────────────────────────────────────────────

def is_file_path(text: str) -> bool:
    p = Path(text.strip())
    return p.suffix.lower() in _SUPPORTED_EXTS and p.exists()


def extract_file_path(text: str) -> str | None:
    """Find the first valid, existing supported file path in *text*."""
    for match in _FILE_RE.finditer(text):
        candidate = match.group(0)
        if Path(candidate).exists():
            return candidate
    return None


# ─────────────────────────────────────────────────────────────
# TEXT EXTRACTION
# ─────────────────────────────────────────────────────────────

def extract_text(file_path: str) -> str:
    ext = Path(file_path).suffix.lower()

    if ext == ".docx":
        doc = Document(file_path)
        return "\n".join(p.text for p in doc.paragraphs if p.text.strip())

    if ext == ".pdf":
        text = ""
        with pdfplumber.open(file_path) as pdf:
            for page in pdf.pages:
                text += (page.extract_text() or "") + "\n"
                tables = page.extract_tables()
                if tables:
                    text += "\n[TABLE DATA]\n"
                    for table in tables:
                        for row in table:
                            text += " | ".join(str(c) if c else "" for c in row) + "\n"
        return text

    if ext in {".txt", ".md"}:
        with open(file_path, "r", encoding="utf-8") as fh:
            return fh.read()

    raise ValueError(f"Unsupported file format: {ext}")


# ─────────────────────────────────────────────────────────────
# SAFE JSON PARSING
# ─────────────────────────────────────────────────────────────

def parse_json_safely(text: str) -> dict:
    try:
        return json.loads(text)
    except Exception:
        pass
    try:
        clean = re.sub(r"```(?:json)?", "", text).strip()
        start = clean.find("{")
        end   = clean.rfind("}") + 1
        if start != -1 and end > start:
            return json.loads(clean[start:end])
    except Exception:
        pass
    logger.warning("[utils] Failed to parse JSON — storing raw output.")
    return {"raw_output": text}


# ─────────────────────────────────────────────────────────────
# COMMAND PARSING
# ─────────────────────────────────────────────────────────────

def parse_command(user_input: str) -> tuple[str | None, list[str]]:
    stripped = user_input.strip()
    if not stripped.startswith("/"):
        return None, []
    parts = stripped.split(maxsplit=2)
    cmd  = parts[0].lower()
    args = parts[1:] if len(parts) > 1 else []
    return cmd, args


# ─────────────────────────────────────────────────────────────
# RECORD FORMATTING
# ─────────────────────────────────────────────────────────────

def format_record(rec: dict) -> str:
    # Try to pull quotation-specific fields from structured data
    data = rec.get("data", {})
    quot_num = data.get("quotation_number", "—")
    vendor   = data.get("vendor_name", "—")
    total    = data.get("grand_total", "—")
    currency = data.get("currency", "")
    validity = data.get("validity_date", "—")

    return (
        f"  ID              : {rec.get('_id')}\n"
        f"  File Name       : {rec.get('file_name')}\n"
        f"  Stored At       : {rec.get('stored_path')}\n"
        f"  Length          : {len(rec.get('raw_text', ''))} chars\n"
        f"  Quotation No.   : {quot_num}\n"
        f"  Vendor          : {vendor}\n"
        f"  Grand Total     : {total} {currency}\n"
        f"  Valid Until     : {validity}\n"
        f"  Created At      : {rec.get('created_at', 'unknown')}\n"
    )


# ─────────────────────────────────────────────────────────────
# DOCUMENT CONTEXT BUILDING
# ─────────────────────────────────────────────────────────────

def build_document_context(documents: dict) -> str:
    if not documents:
        return ""
    sections: list[str] = []
    for i, (fname, doc) in enumerate(documents.items(), 1):
        sections.append(
            f"{'='*60}\n"
            f"DOCUMENT {i}: {fname}\n"
            f"{'='*60}\n"
            f"--- RAW TEXT ---\n"
            f"{doc.get('raw_text', '')[:6000]}\n\n"
            f"--- STRUCTURED DATA ---\n"
            f"{doc.get('structured_json', '')[:2000]}\n"
        )
    return "\n".join(sections)


# ─────────────────────────────────────────────────────────────
# SESSION-STATE HELPERS
# ─────────────────────────────────────────────────────────────

def load_documents_from_state(raw: str) -> dict:
    try:
        return json.loads(raw) if raw else {}
    except Exception:
        return {}


def session_entry_from_record(rec: dict) -> dict:
    return {
        "file_name":       rec.get("file_name", ""),
        "stored_path":     rec.get("stored_path", ""),
        "record_id":       str(rec.get("_id", "")),
        "raw_text":        rec.get("raw_text", ""),
        "structured_json": json.dumps(rec.get("data", {})),
        "uploaded_at":     str(rec.get("created_at", "")),
    }


# ─────────────────────────────────────────────────────────────
# VALIDATION HELPERS
# ─────────────────────────────────────────────────────────────

def validate_file_for_upload(file_path: str) -> str | None:
    """Return an error string if invalid, or None if OK."""
    p = Path(file_path)
    if not p.exists():
        return f"File not found: {file_path}"
    if p.suffix.lower() not in _SUPPORTED_EXTS:
        return (
            f"Unsupported file format: {p.suffix}\n"
            f"Supported formats: {', '.join(sorted(_SUPPORTED_EXTS))}"
        )
    return None
