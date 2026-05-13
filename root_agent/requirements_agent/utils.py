# requirements_agent/utils.py

import re
import sys
import json
from pathlib import Path

# ── Ensure both the agent dir and root dir are on sys.path ──────────────────
_AGENT_DIR = Path(__file__).resolve().parent
_ROOT_DIR  = _AGENT_DIR.parent
for _p in (_AGENT_DIR, _ROOT_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
# ─────────────────────────────────────────────────────────────────────────────

from docx import Document
import pdfplumber

from requirements_agent.logger import get_logger

logger = get_logger(__name__)

SUPPORTED_EXTENSIONS = {".docx", ".pdf", ".txt", ".md"}

# Matches absolute Windows paths, absolute Unix paths, AND relative paths
_FILE_RE = re.compile(
    r'(?:[A-Za-z]:[\\\/][\S]+|\/[\S]+|[\w][\w\/\\.\-]*)\.(?:docx|pdf|txt|md)',
    re.IGNORECASE,
)


# ─────────────────────────────────────────────────────────────
# TEXT EXTRACTION
# ─────────────────────────────────────────────────────────────

def extract_text(file_path: str) -> str:
    ext = Path(file_path).suffix.lower()

    if ext == ".docx":
        doc = Document(file_path)
        return "\n".join([p.text for p in doc.paragraphs if p.text.strip()])

    elif ext == ".pdf":
        text = ""
        with pdfplumber.open(file_path) as pdf:
            for page in pdf.pages:
                text += (page.extract_text() or "") + "\n"
                tables = page.extract_tables()
                if tables:
                    text += "\n[TABLE DATA]\n"
                    for table in tables:
                        for row in table:
                            text += " | ".join([str(c) if c else "" for c in row]) + "\n"
        return text

    elif ext in (".txt", ".md"):
        with open(file_path, "r", encoding="utf-8") as f:
            return f.read()

    else:
        raise ValueError(f"Unsupported file format: {ext}")


# ─────────────────────────────────────────────────────────────
# JSON HELPERS
# ─────────────────────────────────────────────────────────────

def parse_json_safely(text: str) -> dict:
    try:
        return json.loads(text)
    except Exception:
        try:
            clean = re.sub(r"```(?:json)?", "", text).strip()
            start = clean.find("{")
            end   = clean.rfind("}") + 1
            return json.loads(clean[start:end])
        except Exception:
            logger.warning("Failed to parse JSON — storing raw output.")
            return {"raw_output": text}


# ─────────────────────────────────────────────────────────────
# PATH HELPERS
# ─────────────────────────────────────────────────────────────

def is_file_path(text: str) -> bool:
    p = Path(text.strip())
    return p.suffix.lower() in SUPPORTED_EXTENSIONS and p.exists()


def extract_file_path(text: str) -> str | None:
    """Find the first supported file path embedded anywhere in *text*."""
    for match in _FILE_RE.finditer(text):
        candidate = match.group(0)
        if Path(candidate).exists():
            return candidate
    return None


# ─────────────────────────────────────────────────────────────
# FORMATTERS
# ─────────────────────────────────────────────────────────────

def format_record(rec: dict) -> str:
    """Human-readable summary of a single MongoDB record."""
    return (
        f"  ID         : {rec.get('_id')}\n"
        f"  File Name  : {rec.get('file_name')}\n"
        f"  Stored At  : {rec.get('stored_path')}\n"
        f"  Length     : {len(rec.get('raw_text', ''))} chars\n"
        f"  Created At : {rec.get('created_at', 'unknown')}\n"
    )


def build_document_context(documents: dict) -> str:
    if not documents:
        return ""
    context = ""
    for i, (fname, doc) in enumerate(documents.items(), 1):
        context += (
            f"\n{'='*60}\n"
            f"DOCUMENT {i}: {fname}\n"
            f"{'='*60}\n"
            f"--- RAW TEXT ---\n"
            f"{doc['raw_text'][:6000]}\n\n"
            f"--- STRUCTURED DATA ---\n"
            f"{doc['structured_json'][:2000]}\n\n"
        )
    return context
