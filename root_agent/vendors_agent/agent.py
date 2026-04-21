# agent.py  —  Vendors Agent

import json
import datetime
from pathlib import Path
from typing import AsyncGenerator

from google.adk.agents import BaseAgent, LlmAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event, EventActions
from google.adk.apps import App
from google.genai import types

from docx import Document
import pdfplumber

from config import config
from nosql_db import MongoCollection
from logger import get_logger
from file_ops import save_file, delete_file, replace_file, write_text, list_stored_files, file_exists

logger = get_logger(__name__)
MODEL  = config.MODEL

vendors_collection = MongoCollection("vendors")

STORAGE_DIR = Path("VENDORS")


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

    elif ext in [".txt", ".md"]:
        with open(file_path, "r", encoding="utf-8") as f:
            return f.read()

    else:
        raise ValueError(f"Unsupported file format: {ext}")


# ─────────────────────────────────────────────────────────────
# SAFE JSON PARSER
# ─────────────────────────────────────────────────────────────
def parse_json_safely(text: str) -> dict:
    try:
        return json.loads(text)
    except Exception:
        try:
            start = text.find("{")
            end   = text.rfind("}") + 1
            return json.loads(text[start:end])
        except Exception:
            logger.warning("Failed to parse JSON. Storing raw output.")
            return {"raw_output": text}


# ─────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────
def _is_file_path(text: str) -> bool:
    SUPPORTED = {".docx", ".pdf", ".txt", ".md"}
    p = Path(text.strip())
    return p.suffix.lower() in SUPPORTED and p.exists()


def _build_document_context(documents: dict) -> str:
    if not documents:
        return ""
    context = ""
    for i, (fname, doc) in enumerate(documents.items(), 1):
        context += f"""
{'='*60}
DOCUMENT {i}: {fname}
{'='*60}
--- RAW TEXT ---
{doc['raw_text'][:6000]}

--- STRUCTURED DATA ---
{doc['structured_json'][:2000]}

"""
    return context


def _format_record(rec: dict) -> str:
    """Human-readable summary of a single MongoDB record."""
    return (
        f"  ID         : {rec.get('_id')}\n"
        f"  File Name  : {rec.get('file_name')}\n"
        f"  Stored At  : {rec.get('stored_path')}\n"
        f"  Length     : {len(rec.get('raw_text', ''))} chars\n"
        f"  Created At : {rec.get('created_at', 'unknown')}\n"
    )


# ─────────────────────────────────────────────────────────────
# COMMAND PARSER
# ─────────────────────────────────────────────────────────────
# Supported chat commands:
#
#   /list                          – list all DB records
#   /get   <record_id>             – show details of one record
#   /delete <record_id>            – delete record + file
#   /update <record_id> <new_file> – replace file + re-extract + update record
#
# Everything else → upload detection OR Q&A

def _parse_command(user_input: str):
    """Returns (command, args_list) or (None, None) if not a slash command."""
    stripped = user_input.strip()
    if not stripped.startswith("/"):
        return None, None
    parts = stripped.split(maxsplit=2)
    cmd  = parts[0].lower()
    args = parts[1:] if len(parts) > 1 else []
    return cmd, args


# ─────────────────────────────────────────────────────────────
# VENDORS CHATBOT AGENT
# ─────────────────────────────────────────────────────────────
class VendorsChatbot(BaseAgent):

    def __init__(self):
        super().__init__(
            name="vendors_chatbot",
            description=(
                "Ingest multiple vendor documents, answer questions, "
                "and manage records via CRUD commands."
            ),
        )

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:

        logger.info("Vendors Chatbot invoked")

        user_input = ""
        if ctx.user_content and ctx.user_content.parts:
            for part in ctx.user_content.parts:
                if getattr(part, "text", None):
                    user_input += part.text.strip()

        if not user_input:
            yield self._text_event("Please enter a command or a question.")
            return

        cmd, args = _parse_command(user_input)

        if cmd == "/list":
            async for ev in self._handle_list(ctx):
                yield ev

        elif cmd == "/get":
            async for ev in self._handle_get(ctx, args):
                yield ev

        elif cmd == "/delete":
            async for ev in self._handle_delete(ctx, args):
                yield ev

        elif cmd == "/update":
            async for ev in self._handle_update(ctx, args):
                yield ev

        elif cmd == "/help":
            yield self._text_event(_HELP_TEXT)

        elif _is_file_path(user_input):
            async for ev in self._handle_upload(ctx, user_input):
                yield ev

        else:
            async for ev in self._handle_question(ctx, user_input):
                yield ev

    # ─────────────────────────────────────────────────────────
    # CONVENIENCE
    # ─────────────────────────────────────────────────────────
    def _text_event(self, text: str) -> Event:
        return Event(
            author=self.name,
            content=types.Content(
                role="model",
                parts=[types.Part(text=text)],
            ),
        )

    # ─────────────────────────────────────────────────────────
    # /list  — READ ALL
    # ─────────────────────────────────────────────────────────
    async def _handle_list(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:

        logger.info("CRUD: /list")
        try:
            records = vendors_collection.fetch_all()
        except Exception as e:
            yield self._text_event(f"Database error: {e}")
            return

        if not records:
            yield self._text_event(
                "📭 No vendor records found in the database.\n"
                "Upload a vendor document first: /upload <file_path>"
            )
            return

        lines = [f"📋 {len(records)} vendor record(s) in the database:\n"]
        for i, rec in enumerate(records, 1):
            lines.append(f"{'─'*50}")
            lines.append(f"[{i}] Record")
            lines.append(_format_record(rec))

        yield self._text_event("\n".join(lines))

    # ─────────────────────────────────────────────────────────
    # /get <record_id>  — READ ONE
    # ─────────────────────────────────────────────────────────
    async def _handle_get(
        self, ctx: InvocationContext, args: list
    ) -> AsyncGenerator[Event, None]:

        if not args:
            yield self._text_event("Usage: /get <record_id>")
            return

        record_id = args[0].strip()
        logger.info(f"CRUD: /get {record_id}")

        try:
            rec = vendors_collection.fetch_by_id(record_id)
        except Exception as e:
            yield self._text_event(f"Database error: {e}")
            return

        if not rec:
            yield self._text_event(f"No vendor record found with ID: {record_id}")
            return

        structured = rec.get("data", {})
        structured_str = json.dumps(structured, indent=2)[:3000]

        msg = (
            f"📄 Vendor Record Details\n{'='*50}\n"
            f"{_format_record(rec)}\n"
            f"--- Structured Data (truncated to 3000 chars) ---\n"
            f"{structured_str}\n\n"
            f"--- Raw Text Preview (first 500 chars) ---\n"
            f"{rec.get('raw_text', '')[:500]}"
        )
        yield self._text_event(msg)

    # ─────────────────────────────────────────────────────────
    # /delete <record_id>  — DELETE DB RECORD + FILE
    # ─────────────────────────────────────────────────────────
    async def _handle_delete(
        self, ctx: InvocationContext, args: list
    ) -> AsyncGenerator[Event, None]:

        if not args:
            yield self._text_event("Usage: /delete <record_id>")
            return

        record_id = args[0].strip()
        logger.info(f"CRUD: /delete {record_id}")

        # ── 1. Fetch record first so we know the file path ──
        try:
            rec = vendors_collection.fetch_by_id(record_id)
        except Exception as e:
            yield self._text_event(f"Database error: {e}")
            return

        if not rec:
            yield self._text_event(f"No vendor record found with ID: {record_id}")
            return

        stored_path = rec.get("stored_path", "")
        file_name   = rec.get("file_name", "unknown")

        # ── 2. Delete the physical file ──
        file_deleted = False
        file_msg = ""
        if stored_path:
            try:
                file_deleted = delete_file(stored_path, str(STORAGE_DIR))
                file_msg = (
                    f"🗑  File deleted  : {stored_path}"
                    if file_deleted
                    else f"⚠️  File not found on disk (already removed?): {stored_path}"
                )
            except ValueError as ve:
                file_msg = f"⚠️  File deletion skipped: {ve}"
            except Exception as e:
                file_msg = f"⚠️  File deletion failed: {e}"
        else:
            file_msg = "⚠️  No stored_path in record; skipping file deletion."

        # ── 3. Delete the DB record ──
        try:
            vendors_collection.delete_by_id(record_id)
            db_msg = f"✅ DB record deleted : {record_id}"
        except Exception as e:
            yield self._text_event(
                f"{file_msg}\nDB deletion failed: {e}\n"
                "The file may have been removed but the record still exists."
            )
            return

        # ── 4. Remove from session state if present ──
        existing_docs_raw = ctx.session.state.get("documents", "{}")
        try:
            existing_docs = json.loads(existing_docs_raw)
        except Exception:
            existing_docs = {}

        removed_key = None
        for key, val in list(existing_docs.items()):
            if val.get("record_id") == record_id:
                removed_key = key
                del existing_docs[key]
                break

        session_msg = (
            f"🗂  Removed from session: {removed_key}"
            if removed_key
            else "ℹ️  Document was not in the current session state."
        )

        yield Event(
            author=self.name,
            content=types.Content(
                role="model",
                parts=[types.Part(text=(
                    f"🗑  Delete complete for: {file_name}\n\n"
                    f"{db_msg}\n"
                    f"{file_msg}\n"
                    f"{session_msg}"
                ))],
            ),
            actions=EventActions(
                state_delta={"documents": json.dumps(existing_docs)}
            ),
        )

    # ─────────────────────────────────────────────────────────
    # /update <record_id> <new_file_path>  — UPDATE DB + FILE
    # ─────────────────────────────────────────────────────────
    async def _handle_update(
        self, ctx: InvocationContext, args: list
    ) -> AsyncGenerator[Event, None]:

        if len(args) < 2:
            yield self._text_event(
                "Usage: /update <record_id> <new_file_path>\n\n"
                "Provide the record ID and the path to the revised vendor document.\n"
                "The stored file and the DB record will both be updated."
            )
            return

        record_id     = args[0].strip()
        new_file_path = args[1].strip()
        logger.info(f"CRUD: /update {record_id} ← {new_file_path}")

        if not _is_file_path(new_file_path):
            yield self._text_event(
                f"File not found or unsupported format: {new_file_path}\n"
                "Supported: .docx, .pdf, .txt, .md"
            )
            return

        try:
            rec = vendors_collection.fetch_by_id(record_id)
        except Exception as e:
            yield self._text_event(f"Database error: {e}")
            return

        if not rec:
            yield self._text_event(f"No vendor record found with ID: {record_id}")
            return

        stored_path   = rec.get("stored_path", "")
        old_file_name = rec.get("file_name", "unknown")

        try:
            replace_file(stored_path, new_file_path, str(STORAGE_DIR))
            file_msg = f"✅ File replaced  : {stored_path}"
        except Exception as e:
            yield self._text_event(f"File replacement failed: {e}")
            return

        try:
            new_text = extract_text(stored_path)
        except Exception as e:
            yield self._text_event(f"Text extraction failed: {e}")
            return

        if not new_text.strip():
            yield self._text_event("The new file appears to be empty or unreadable.")
            return

        struct_agent = LlmAgent(
            name="dynamic_structurer",
            model=MODEL,
            instruction=f"""
You are an intelligent document parser specialised in vendor documents.

Extract ALL meaningful structured information from the vendor document.

Rules:
- Do NOT use a fixed schema
- Create dynamic JSON based on content
- Preserve hierarchy
- Extract vendor name, contact details, product categories, pricing,
  certifications, payment terms, delivery terms, and any other key data

Return ONLY valid JSON.

DOCUMENT:
{new_text[:12000]}
""",
        )

        structured_text = ""
        async for event in struct_agent.run_async(ctx):
            if event.is_final_response() and event.content:
                for part in event.content.parts:
                    if getattr(part, "text", None):
                        structured_text += part.text

        structured_json = parse_json_safely(structured_text)

        update_payload = {
            "raw_text":   new_text,
            "data":       structured_json,
            "updated_at": datetime.datetime.utcnow(),
            "file_name":  Path(stored_path).name,
        }

        try:
            vendors_collection.update_by_id(record_id, update_payload)
            db_msg = f"✅ DB record updated : {record_id}"
        except Exception as e:
            yield self._text_event(
                f"{file_msg}\nDB update failed: {e}\n"
                "File was replaced but DB record could not be updated."
            )
            return

        # ── Sync session state ──
        existing_docs_raw = ctx.session.state.get("documents", "{}")
        try:
            existing_docs = json.loads(existing_docs_raw)
        except Exception:
            existing_docs = {}

        session_key = None
        for key, val in existing_docs.items():
            if val.get("record_id") == record_id:
                session_key = key
                existing_docs[key]["raw_text"]        = new_text
                existing_docs[key]["structured_json"] = json.dumps(structured_json)
                existing_docs[key]["uploaded_at"]     = datetime.datetime.utcnow().isoformat()
                break

        session_msg = (
            f"🗂  Session state refreshed for: {session_key}"
            if session_key
            else "ℹ️  Record was not in current session — session state unchanged."
        )

        yield Event(
            author=self.name,
            content=types.Content(
                role="model",
                parts=[types.Part(text=(
                    f"🔄 Update complete for: {old_file_name}\n\n"
                    f"{file_msg}\n"
                    f"{db_msg}\n"
                    f"{session_msg}\n\n"
                    f"New content length : {len(new_text)} characters\n"
                    f"Structured keys    : {list(structured_json.keys())}\n\n"
                    f"You can now ask questions about the updated vendor document."
                ))],
            ),
            actions=EventActions(
                state_delta={"documents": json.dumps(existing_docs)}
            ),
        )

    # ─────────────────────────────────────────────────────────
    # UPLOAD HANDLER
    # ─────────────────────────────────────────────────────────
    async def _handle_upload(
        self, ctx: InvocationContext, file_path: str
    ) -> AsyncGenerator[Event, None]:

        logger.info(f"Handling vendor file upload: {file_path}")

        try:
            stored_path = save_file(file_path, str(STORAGE_DIR))
        except Exception as e:
            logger.error(f"File save failed: {e}")
            yield self._text_event(f"File save failed: {e}")
            return

        try:
            text_content = extract_text(stored_path)
        except Exception as e:
            logger.error(f"Text extraction failed: {e}")
            yield self._text_event(f"Text extraction failed: {e}")
            return

        if not text_content.strip():
            yield self._text_event("File is empty or unreadable.")
            return

        logger.info(f"Extracted text length: {len(text_content)}")

        struct_agent = LlmAgent(
            name="dynamic_structurer",
            model=MODEL,
            instruction=f"""
You are an intelligent document parser specialised in vendor documents.

Extract ALL meaningful structured information from the vendor document.

Rules:
- Do NOT use a fixed schema
- Create dynamic JSON based on content
- Preserve hierarchy
- Extract vendor name, contact details, product categories, pricing,
  certifications, payment terms, delivery terms, and any other key data

Return ONLY valid JSON.

DOCUMENT:
{text_content[:12000]}
""",
        )

        structured_text = ""
        async for event in struct_agent.run_async(ctx):
            if event.is_final_response() and event.content:
                for part in event.content.parts:
                    if getattr(part, "text", None):
                        structured_text += part.text

        structured_json = parse_json_safely(structured_text)
        logger.debug(f"Structured JSON keys: {list(structured_json.keys())}")

        file_name = Path(stored_path).name
        record = {
            "file_name":   file_name,
            "stored_path": stored_path,
            "raw_text":    text_content,
            "data":        structured_json,
            "created_at":  datetime.datetime.utcnow(),
        }

        try:
            inserted_id = vendors_collection.insert_one(record)
        except Exception as e:
            logger.error(f"MongoDB insert failed: {e}")
            yield self._text_event("Database insert failed.")
            return

        logger.info(f"Stored vendor document with ID: {inserted_id}")

        existing_docs_raw = ctx.session.state.get("documents", "{}")
        try:
            existing_docs = json.loads(existing_docs_raw)
        except Exception:
            existing_docs = {}

        original_name = Path(file_path).name
        existing_docs[original_name] = {
            "file_name":       file_name,
            "stored_path":     stored_path,
            "record_id":       str(inserted_id),
            "raw_text":        text_content,
            "structured_json": json.dumps(structured_json),
            "uploaded_at":     datetime.datetime.utcnow().isoformat(),
        }

        doc_list = "\n".join(
            [f"  {i+1}. {name}" for i, name in enumerate(existing_docs.keys())]
        )

        yield Event(
            author=self.name,
            content=types.Content(
                role="model",
                parts=[types.Part(text=(
                    f"✅ Vendor document uploaded successfully.\n\n"
                    f"File     : {original_name}\n"
                    f"Record ID: {inserted_id}\n"
                    f"Length   : {len(text_content)} characters\n\n"
                    f"📂 Vendor documents loaded in this session ({len(existing_docs)}):\n"
                    f"{doc_list}\n\n"
                    f"You can now ask questions about any of these vendor documents.\n"
                    f"Use /list to see all DB records, /help for all commands."
                ))],
            ),
            actions=EventActions(
                state_delta={
                    "documents":      json.dumps(existing_docs),
                    "last_file_name": original_name,
                }
            ),
        )

    # ─────────────────────────────────────────────────────────
    # QUESTION / NL QUERY HANDLER
    # ─────────────────────────────────────────────────────────
    async def _handle_question(
        self, ctx: InvocationContext, question: str
    ) -> AsyncGenerator[Event, None]:

        logger.info(f"Handling vendor question: {question}")

        # ── 1. Try session state first ──
        existing_docs_raw = ctx.session.state.get("documents", "{}")
        try:
            documents = json.loads(existing_docs_raw)
        except Exception:
            documents = {}

        # ── 2. Fall back to MongoDB if session is empty ──
        state_delta = {}
        if not documents:
            logger.info("Session empty — fetching all vendor records from MongoDB for Q&A.")
            try:
                records = vendors_collection.fetch_all()
            except Exception as e:
                yield self._text_event(f"Database error while loading documents: {e}")
                return

            if not records:
                yield self._text_event(
                    "No vendor records found in the database.\n"
                    "Upload a vendor document first using its file path or /upload <file_path>."
                )
                return

            for rec in records:
                original_name = rec.get("file_name", rec.get("_id", "unknown"))
                documents[original_name] = {
                    "file_name":       rec.get("file_name", ""),
                    "stored_path":     rec.get("stored_path", ""),
                    "record_id":       str(rec.get("_id", "")),
                    "raw_text":        rec.get("raw_text", ""),
                    "structured_json": json.dumps(rec.get("data", {})),
                    "uploaded_at":     str(rec.get("created_at", "")),
                }

            logger.info(f"Loaded {len(documents)} vendor document(s) from DB into session.")
            state_delta["documents"] = json.dumps(documents)

        doc_context = _build_document_context(documents)
        doc_names   = list(documents.keys())

        # ── 3. Run QA agent ──
        qa_agent = LlmAgent(
            name="vendor_qa_agent",
            model=MODEL,
            instruction=f"""
You are an expert Vendor Management assistant with deep knowledge of procurement and supplier management.

You have access to {len(documents)} vendor document(s):
{chr(10).join([f"  - {n}" for n in doc_names])}

You can answer ANY natural language question about these documents, including:

LOOKUP & DETAIL questions
  - "What are the contact details of Dell India?"
  - "What products does the printer vendor supply?"
  - "Show me the payment terms for vendor XYZ."

FILTERING & CATEGORY questions
  - "List all vendors that supply electronics."
  - "Which vendors provide home appliances?"
  - "Show me all vendors with ISO certification."
  - "Which vendors are marked as preferred?"

COUNTING & AGGREGATION questions
  - "How many vendors are registered in total?"
  - "How many vendors supply laptops?"
  - "How many vendors offer credit payment terms?"

PRICING & COMMERCIAL questions
  - "What is the unit price offered by each vendor for laptops?"
  - "Which vendor offers the best pricing for network switches?"
  - "List all vendors with a minimum order quantity above 10 units."

CROSS-DOCUMENT ANALYSIS questions
  - "Compare the warranty terms across all vendors."
  - "Which vendor has the shortest delivery lead time?"
  - "What certifications do our vendors hold?"

GUIDELINES:
- Always cite WHICH document your answer comes from.
- For counting/aggregation, scan ALL documents and sum where relevant.
- For filtering, scan ALL documents and return only matching ones.
- If a question spans multiple documents, address each one then give a combined summary.
- If the answer is not found in any document, say so clearly.
- Present answers in a clean, structured format (use bullet points or tables where helpful).

--- VENDOR DOCUMENTS CONTEXT ---
{doc_context}
""",
        )

        if state_delta:
            yield Event(
                author=self.name,
                content=types.Content(role="model", parts=[types.Part(text="")]),
                actions=EventActions(state_delta=state_delta),
            )

        async for event in qa_agent.run_async(ctx):
            if event.is_final_response() and event.content:
                yield event


# ─────────────────────────────────────────────────────────────
# HELP TEXT
# ─────────────────────────────────────────────────────────────
_HELP_TEXT = """
📖 Vendors Chatbot — Commands
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

UPLOAD  (file stored in VENDORS/ with a timestamp prefix)
  <file_path>                      Type or paste the file path directly
  /upload <file_path>              Alternatively, use the /upload prefix
  Supported: .docx, .pdf, .txt, .md

READ
  /list                            List all vendor records in the database
  /get <record_id>                 Show full details of one vendor record

DELETE  (removes record + stored file — asks for confirmation)
  /delete <record_id>

UPDATE  (replaces stored file, re-extracts text, updates DB record)
  /update <record_id> <new_file>

NATURAL LANGUAGE Q&A  (works even without uploading in this session)
  Ask anything about your vendors in plain English, for example:
  · List all vendors that supply electronics
  · Which vendors are ISO certified?
  · What are the payment terms for Dell India?
  · Which vendor offers the lowest price for laptops?
  · Compare delivery lead times across all vendors
  · How many vendors supply home appliances?

MISC
  /help                            Show this message
"""


# ─────────────────────────────────────────────────────────────
# EXPORTS
# ─────────────────────────────────────────────────────────────
chatbot_agent = VendorsChatbot()

app = App(
    name="vendors_chatbot_app",
    root_agent=chatbot_agent,
)

__all__ = ["chatbot_agent", "app"]
