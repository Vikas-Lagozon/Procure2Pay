# subagent.py  —  Vendors Sub-Agent

import json
import datetime
from pathlib import Path
from typing import AsyncGenerator

from google.adk.agents import BaseAgent, LlmAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event, EventActions
from google.genai import types

from docx import Document
import pdfplumber

from config import config
from nosql_db import MongoCollection
from logger import get_logger
from file_ops import save_file, delete_file, replace_file, list_stored_files, file_exists

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
    return (
        f"  ID         : {rec.get('_id')}\n"
        f"  File Name  : {rec.get('file_name')}\n"
        f"  Stored At  : {rec.get('stored_path')}\n"
        f"  Length     : {len(rec.get('raw_text', ''))} chars\n"
        f"  Created At : {rec.get('created_at', 'unknown')}\n"
    )


def _structured_response(human_text: str, data: dict) -> str:
    """
    Combine a human-readable message with an embedded JSON block.
    Root agents can extract the JSON block; human users read the text.
    """
    return (
        f"{human_text}\n\n"
        f"```json\n{json.dumps(data, indent=2, default=str)}\n```"
    )


# ─────────────────────────────────────────────────────────────
# SLASH COMMAND PARSER  (backward compat)
# ─────────────────────────────────────────────────────────────
def _parse_slash_command(user_input: str):
    """Returns (command, args_list) or (None, None)."""
    stripped = user_input.strip()
    if not stripped.startswith("/"):
        return None, None
    parts = stripped.split(maxsplit=2)
    cmd  = parts[0].lower()
    args = parts[1:] if len(parts) > 1 else []
    return cmd, args


# ─────────────────────────────────────────────────────────────
# NL INTENT ROUTER PROMPT
# ─────────────────────────────────────────────────────────────
_INTENT_ROUTER_PROMPT = """
You are an intent classifier for a Vendor Management agent.

Classify the user message into exactly ONE of these intents and extract
any parameters. Return ONLY valid JSON — no explanation, no markdown fences.

INTENTS
-------
list      — user wants to see all stored vendor records
get       — user wants details of one specific vendor record (needs record_id)
delete    — user wants to delete a specific vendor record (needs record_id)
update    — user wants to replace/update a vendor record with a new file
             (needs record_id AND file_path)
upload    — user wants to ingest/add a new vendor document (needs file_path)
qa        — any analytical, lookup, or question-answering request about
             vendor content (pricing, certifications, categories, contacts, etc.)

PARAMETER EXTRACTION RULES
---------------------------
record_id : hex string that looks like a MongoDB ObjectId (24 hex chars)
file_path : any Windows or Unix file path ending in .docx/.pdf/.txt/.md
question  : for qa intent — copy the full user message verbatim

OUTPUT FORMAT (always return this exact shape)
-----------------------------------------------
{
  "intent":    "list|get|delete|update|upload|qa",
  "record_id": "<24-char hex string or null>",
  "file_path": "<file path string or null>",
  "question":  "<verbatim user message if intent=qa, else null>"
}

EXAMPLES
--------
"list all vendors"
→ {"intent":"list","record_id":null,"file_path":null,"question":null}

"show me vendor record 69e6ee3701f7c06f1ec81fce"
→ {"intent":"get","record_id":"69e6ee3701f7c06f1ec81fce","file_path":null,"question":null}

"delete vendor 69e6ef8801f7c06f1ec81fd0"
→ {"intent":"delete","record_id":"69e6ef8801f7c06f1ec81fd0","file_path":null,"question":null}

"update record 69e6ee3701f7c06f1ec81fce with D:\\docs\\dell_v2.docx"
→ {"intent":"update","record_id":"69e6ee3701f7c06f1ec81fce","file_path":"D:\\docs\\dell_v2.docx","question":null}

"ingest the vendor file at D:\\docs\\hp_vendor.docx"
→ {"intent":"upload","record_id":null,"file_path":"D:\\docs\\hp_vendor.docx","question":null}

"which vendors supply laptops?"
→ {"intent":"qa","record_id":null,"file_path":null,"question":"which vendors supply laptops?"}

"what are the payment terms for Dell India?"
→ {"intent":"qa","record_id":null,"file_path":null,"question":"what are the payment terms for Dell India?"}
"""


# ─────────────────────────────────────────────────────────────
# VENDORS SUB-AGENT
# ─────────────────────────────────────────────────────────────
class VendorsSubAgent(BaseAgent):
    """
    Sub-agent that manages vendor documents for procurement workflows.

    CAPABILITIES (invoke this agent when the user asks to):
    ─────────────────────────────────────────────────────────
    • LIST    — show all stored vendor records
    • GET     — retrieve full details of one vendor record by ID
    • DELETE  — permanently remove a vendor record and its stored file
    • UPDATE  — replace a stored vendor document and re-extract its content
    • UPLOAD  — ingest a new .docx / .pdf / .txt / .md vendor document
    • Q&A     — answer natural language questions about vendor content:
                 - filter vendors by product category or certification
                 - look up contact details, pricing, payment terms
                 - compare delivery times or warranty terms across vendors
                 - count vendors by category or attribute
                 - identify preferred or certified vendors

    ACCEPTS BOTH:
    • Slash commands  : /list  /get <id>  /delete <id>
                        /update <id> <path>  /upload <path>
    • Natural language: "list all vendors"
                        "which vendors supply electronics?"
                        "delete vendor record 69e6ef8801f7c06f1ec81fd0"
                        "ingest D:\\docs\\hp_vendor.docx"

    RESPONSES include a machine-readable JSON block so the calling
    root agent can parse structured results for further processing.
    """

    def __init__(self):
        super().__init__(
            name="vendors_sub_agent",
            description=(
                "Manages vendor documents stored in MongoDB. "
                "Can list, retrieve, delete, update, and upload vendor files "
                "(.docx/.pdf/.txt/.md). Also answers natural language questions "
                "about vendor content such as product categories, pricing, "
                "certifications, payment terms, delivery timelines, and "
                "preferred vendor identification. "
                "Use this agent for ANY task related to vendor management."
            ),
        )

    # ─────────────────────────────────────────────────────────
    # MAIN ENTRY POINT
    # ─────────────────────────────────────────────────────────
    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:

        logger.info("VendorsSubAgent invoked")

        user_input = ""
        if ctx.user_content and ctx.user_content.parts:
            for part in ctx.user_content.parts:
                if getattr(part, "text", None):
                    user_input += part.text.strip()

        if not user_input:
            yield self._text_event("No input received.")
            return

        # ── Try slash command first (backward compat) ──
        cmd, args = _parse_slash_command(user_input)

        if cmd == "/list":
            async for ev in self._handle_list(ctx):
                yield ev
            return

        elif cmd == "/get":
            async for ev in self._handle_get(ctx, args):
                yield ev
            return

        elif cmd == "/delete":
            async for ev in self._handle_delete(ctx, args):
                yield ev
            return

        elif cmd == "/update":
            async for ev in self._handle_update(ctx, args):
                yield ev
            return

        elif cmd == "/upload":
            file_path = args[0].strip() if args else ""
            if not file_path:
                yield self._text_event("Usage: /upload <file_path>")
                return
            async for ev in self._handle_upload(ctx, file_path):
                yield ev
            return

        elif cmd == "/help":
            yield self._text_event(self.description)
            return

        # ── No slash command — run NL intent router ──
        intent_data = await self._detect_intent(ctx, user_input)
        intent    = intent_data.get("intent", "qa")
        record_id = intent_data.get("record_id")
        file_path = intent_data.get("file_path")
        question  = intent_data.get("question") or user_input

        logger.info(f"Detected intent: {intent} | record_id={record_id} | file_path={file_path}")

        if intent == "list":
            async for ev in self._handle_list(ctx):
                yield ev

        elif intent == "get":
            if not record_id:
                yield self._text_event(
                    "I understood you want to get a vendor record, but I could not "
                    "find a record ID in your message. Please provide the 24-character record ID."
                )
            else:
                async for ev in self._handle_get(ctx, [record_id]):
                    yield ev

        elif intent == "delete":
            if not record_id:
                yield self._text_event(
                    "I understood you want to delete a vendor record, but I could not "
                    "find a record ID in your message. Please provide the 24-character record ID."
                )
            else:
                async for ev in self._handle_delete(ctx, [record_id]):
                    yield ev

        elif intent == "update":
            if not record_id or not file_path:
                yield self._text_event(
                    "I understood you want to update a vendor record, but I need both "
                    "a record ID and a new file path. Please provide both."
                )
            else:
                async for ev in self._handle_update(ctx, [record_id, file_path]):
                    yield ev

        elif intent == "upload":
            if not file_path:
                yield self._text_event(
                    "I understood you want to upload a vendor file, but I could not "
                    "find a file path in your message. Please provide the full file path."
                )
            else:
                async for ev in self._handle_upload(ctx, file_path):
                    yield ev

        else:  # qa (default)
            async for ev in self._handle_question(ctx, question):
                yield ev

    # ─────────────────────────────────────────────────────────
    # NL INTENT DETECTION
    # ─────────────────────────────────────────────────────────
    async def _detect_intent(self, ctx: InvocationContext, user_input: str) -> dict:
        router = LlmAgent(
            name="intent_router",
            model=MODEL,
            instruction=_INTENT_ROUTER_PROMPT,
        )

        raw = ""
        try:
            async for event in router.run_async(ctx):
                if event.is_final_response() and event.content:
                    for part in event.content.parts:
                        if getattr(part, "text", None):
                            raw += part.text
        except Exception as e:
            logger.warning(f"Intent router failed: {e}")
            return {"intent": "qa", "record_id": None, "file_path": None, "question": user_input}

        result = parse_json_safely(raw)
        if "intent" not in result:
            return {"intent": "qa", "record_id": None, "file_path": None, "question": user_input}
        return result

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

        logger.info("CRUD: list vendors")
        try:
            records = vendors_collection.fetch_all()
        except Exception as e:
            yield self._text_event(f"Database error: {e}")
            return

        if not records:
            data = {"status": "empty", "count": 0, "records": []}
            yield self._text_event(
                _structured_response(
                    "📭 No vendor records found in the database.",
                    data,
                )
            )
            return

        summary = [
            {
                "id":           rec.get("_id"),
                "file_name":    rec.get("file_name"),
                "stored_path":  rec.get("stored_path"),
                "length_chars": len(rec.get("raw_text", "")),
                "created_at":   str(rec.get("created_at", "")),
            }
            for rec in records
        ]

        lines = [f"📋 {len(records)} vendor record(s) in the database:\n"]
        for i, rec in enumerate(records, 1):
            lines.append(f"{'─'*50}")
            lines.append(f"[{i}] Record")
            lines.append(_format_record(rec))

        yield self._text_event(
            _structured_response(
                "\n".join(lines),
                {"status": "ok", "count": len(records), "records": summary},
            )
        )

    # ─────────────────────────────────────────────────────────
    # /get <record_id>  — READ ONE
    # ─────────────────────────────────────────────────────────
    async def _handle_get(
        self, ctx: InvocationContext, args: list
    ) -> AsyncGenerator[Event, None]:

        if not args:
            yield self._text_event("Please provide a vendor record ID.")
            return

        record_id = args[0].strip()
        logger.info(f"CRUD: get vendor {record_id}")

        try:
            rec = vendors_collection.fetch_by_id(record_id)
        except Exception as e:
            yield self._text_event(f"Database error: {e}")
            return

        if not rec:
            yield self._text_event(
                _structured_response(
                    f"No vendor record found with ID: {record_id}",
                    {"status": "not_found", "record_id": record_id},
                )
            )
            return

        structured = rec.get("data", {})
        structured_str = json.dumps(structured, indent=2)[:3000]

        human_msg = (
            f"📄 Vendor Record Details\n{'='*50}\n"
            f"{_format_record(rec)}\n"
            f"--- Structured Data (truncated to 3000 chars) ---\n"
            f"{structured_str}\n\n"
            f"--- Raw Text Preview (first 500 chars) ---\n"
            f"{rec.get('raw_text', '')[:500]}"
        )

        yield self._text_event(
            _structured_response(
                human_msg,
                {
                    "status":           "ok",
                    "record_id":        record_id,
                    "file_name":        rec.get("file_name"),
                    "stored_path":      rec.get("stored_path"),
                    "created_at":       str(rec.get("created_at", "")),
                    "data":             structured,
                    "raw_text_preview": rec.get("raw_text", "")[:500],
                },
            )
        )

    # ─────────────────────────────────────────────────────────
    # /delete <record_id>  — DELETE DB RECORD + FILE
    # ─────────────────────────────────────────────────────────
    async def _handle_delete(
        self, ctx: InvocationContext, args: list
    ) -> AsyncGenerator[Event, None]:

        if not args:
            yield self._text_event("Please provide a vendor record ID to delete.")
            return

        record_id = args[0].strip()
        logger.info(f"CRUD: delete vendor {record_id}")

        try:
            rec = vendors_collection.fetch_by_id(record_id)
        except Exception as e:
            yield self._text_event(f"Database error: {e}")
            return

        if not rec:
            yield self._text_event(
                _structured_response(
                    f"No vendor record found with ID: {record_id}",
                    {"status": "not_found", "record_id": record_id},
                )
            )
            return

        stored_path = rec.get("stored_path", "")
        file_name   = rec.get("file_name", "unknown")

        file_deleted = False
        file_msg = ""
        if stored_path:
            try:
                file_deleted = delete_file(stored_path, str(STORAGE_DIR))
                file_msg = (
                    f"🗑  File deleted  : {stored_path}"
                    if file_deleted
                    else f"⚠️  File not found on disk: {stored_path}"
                )
            except ValueError as ve:
                file_msg = f"⚠️  File deletion skipped: {ve}"
            except Exception as e:
                file_msg = f"⚠️  File deletion failed: {e}"
        else:
            file_msg = "⚠️  No stored_path in record; skipping file deletion."

        try:
            vendors_collection.delete_by_id(record_id)
            db_msg = f"✅ DB record deleted : {record_id}"
        except Exception as e:
            yield self._text_event(
                _structured_response(
                    f"{file_msg}\nDB deletion failed: {e}",
                    {"status": "partial_failure", "record_id": record_id,
                     "file_deleted": file_deleted, "db_deleted": False},
                )
            )
            return

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

        human_msg = (
            f"🗑  Delete complete for: {file_name}\n\n"
            f"{db_msg}\n{file_msg}\n{session_msg}"
        )

        yield Event(
            author=self.name,
            content=types.Content(
                role="model",
                parts=[types.Part(text=_structured_response(
                    human_msg,
                    {
                        "status":       "ok",
                        "record_id":    record_id,
                        "file_name":    file_name,
                        "file_deleted": file_deleted,
                        "db_deleted":   True,
                    },
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
                "Please provide both a vendor record ID and a new file path to update."
            )
            return

        record_id     = args[0].strip()
        new_file_path = args[1].strip()
        logger.info(f"CRUD: update vendor {record_id} ← {new_file_path}")

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
            yield self._text_event(
                _structured_response(
                    f"No vendor record found with ID: {record_id}",
                    {"status": "not_found", "record_id": record_id},
                )
            )
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
                _structured_response(
                    f"{file_msg}\nDB update failed: {e}",
                    {"status": "partial_failure", "record_id": record_id,
                     "file_replaced": True, "db_updated": False},
                )
            )
            return

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

        human_msg = (
            f"🔄 Update complete for: {old_file_name}\n\n"
            f"{file_msg}\n{db_msg}\n{session_msg}\n\n"
            f"New content length : {len(new_text)} characters\n"
            f"Structured keys    : {list(structured_json.keys())}"
        )

        yield Event(
            author=self.name,
            content=types.Content(
                role="model",
                parts=[types.Part(text=_structured_response(
                    human_msg,
                    {
                        "status":          "ok",
                        "record_id":       record_id,
                        "file_name":       Path(stored_path).name,
                        "file_replaced":   True,
                        "db_updated":      True,
                        "content_length":  len(new_text),
                        "structured_keys": list(structured_json.keys()),
                    },
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

        human_msg = (
            f"✅ Vendor document uploaded successfully.\n\n"
            f"File     : {original_name}\n"
            f"Record ID: {inserted_id}\n"
            f"Length   : {len(text_content)} characters"
        )

        yield Event(
            author=self.name,
            content=types.Content(
                role="model",
                parts=[types.Part(text=_structured_response(
                    human_msg,
                    {
                        "status":          "ok",
                        "record_id":       str(inserted_id),
                        "file_name":       original_name,
                        "stored_path":     stored_path,
                        "content_length":  len(text_content),
                        "structured_keys": list(structured_json.keys()),
                    },
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

        existing_docs_raw = ctx.session.state.get("documents", "{}")
        try:
            documents = json.loads(existing_docs_raw)
        except Exception:
            documents = {}

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
                    _structured_response(
                        "No vendor records found in the database.",
                        {"status": "empty"},
                    )
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

        qa_agent = LlmAgent(
            name="vendor_qa_agent",
            model=MODEL,
            instruction=f"""
You are an expert Vendor Management assistant with deep knowledge of procurement and supplier management.

You have access to {len(documents)} vendor document(s):
{chr(10).join([f"  - {n}" for n in doc_names])}

You can answer ANY natural language question, including:
- Lookup & detail     : "What are the contact details of Dell India?"
- Filtering           : "List all vendors that supply electronics."
- Certifications      : "Which vendors are ISO certified?"
- Pricing & commercial: "Which vendor offers the lowest price for laptops?"
- Counting            : "How many vendors supply home appliances?"
- Cross-doc analysis  : "Compare the warranty terms across all vendors."

GUIDELINES:
- Always cite WHICH document your answer comes from.
- For counting/aggregation, scan ALL documents and sum where relevant.
- For filtering, return only matching documents.
- Present answers in clean structured format (bullet points or tables where helpful).
- If the answer is not in any document, say so clearly.

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
# EXPORT  (only the agent instance — no App, no run loop)
# ─────────────────────────────────────────────────────────────
vendors_sub_agent = VendorsSubAgent()

__all__ = ["vendors_sub_agent"]
