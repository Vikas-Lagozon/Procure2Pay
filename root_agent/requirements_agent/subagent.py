# subagent.py
"""
RequirementsSubAgent — drop-in sub-agent for any Google ADK root agent.

Differences from agent.py (standalone):
  1. NL intent router   — accepts natural language from root agents in
                          addition to slash commands.
  2. Richer description — root agent uses this to decide when to call
                          this sub-agent.
  3. Structured responses — every handler embeds a JSON block so the
                          root agent can parse results programmatically.
  4. No App / run loop  — only the agent instance is exported.
"""

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

requirements_collection = MongoCollection("requirements")
STORAGE_DIR = Path("REQUIREMENTS")


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
# SLASH COMMAND PARSER  (backward compat — used by run.py / human)
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
# NL INTENT ROUTER
# ─────────────────────────────────────────────────────────────
_INTENT_ROUTER_PROMPT = """
You are an intent classifier for a Requirements Management agent.

Classify the user message into exactly ONE of these intents and extract
any parameters. Return ONLY valid JSON — no explanation, no markdown fences.

INTENTS
-------
list      — user wants to see all stored requirement records
get       — user wants details of one specific record (needs record_id)
delete    — user wants to delete a specific record (needs record_id)
update    — user wants to replace/update a record with a new file
             (needs record_id AND file_path)
upload    — user wants to ingest/add a new document (needs file_path)
qa        — any analytical, lookup, or question-answering request about
             requirement content (e.g. counts, specs, categories, units)

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
"list all requirements"
→ {"intent":"list","record_id":null,"file_path":null,"question":null}

"show me requirement 69e6ee3701f7c06f1ec81fce"
→ {"intent":"get","record_id":"69e6ee3701f7c06f1ec81fce","file_path":null,"question":null}

"delete record 69e6ef8801f7c06f1ec81fd0"
→ {"intent":"delete","record_id":"69e6ef8801f7c06f1ec81fd0","file_path":null,"question":null}

"update requirement 69e6ee3701f7c06f1ec81fce using D:\\docs\\req_v2.docx"
→ {"intent":"update","record_id":"69e6ee3701f7c06f1ec81fce","file_path":"D:\\docs\\req_v2.docx","question":null}

"ingest the file at D:\\docs\\laptop_req.docx"
→ {"intent":"upload","record_id":null,"file_path":"D:\\docs\\laptop_req.docx","question":null}

"how many laptop requirements do we have?"
→ {"intent":"qa","record_id":null,"file_path":null,"question":"how many laptop requirements do we have?"}

"what are the technical specifications of the printer requirement?"
→ {"intent":"qa","record_id":null,"file_path":null,"question":"what are the technical specifications of the printer requirement?"}
"""


# ─────────────────────────────────────────────────────────────
# REQUIREMENTS SUB-AGENT
# ─────────────────────────────────────────────────────────────
class RequirementsSubAgent(BaseAgent):
    """
    Sub-agent that manages procurement requirement documents.

    CAPABILITIES (invoke this agent when the user asks to):
    ─────────────────────────────────────────────────────────
    • LIST    — show all stored requirement records
    • GET     — retrieve full details of one record by ID
    • DELETE  — permanently remove a record and its stored file
    • UPDATE  — replace a stored document and re-extract its content
    • UPLOAD  — ingest a new .docx / .pdf / .txt / .md requirement file
    • Q&A     — answer natural language questions about requirement content:
                 - count or filter requirements by category / department
                 - look up quantities, unit prices, vendor details
                 - compare technical specifications across documents
                 - any analytical question about procurement requirements

    ACCEPTS BOTH:
    • Slash commands  : /list  /get <id>  /delete <id>
                        /update <id> <path>  /upload <path>
    • Natural language: "list all requirements"
                        "how many laptop units do we need?"
                        "delete requirement 69e6ef8801f7c06f1ec81fd0"
                        "ingest D:\\docs\\req.docx"

    RESPONSES include a machine-readable JSON block so the calling
    root agent can parse structured results for further processing.
    """

    def __init__(self):
        super().__init__(
            name="requirements_sub_agent",
            description=(
                "Manages procurement requirement documents stored in MongoDB. "
                "Can list, retrieve, delete, update, and upload requirement files "
                "(.docx/.pdf/.txt/.md). Also answers natural language questions "
                "about requirement content such as item counts, unit prices, "
                "technical specs, vendor details, and category-based filtering. "
                "Use this agent for ANY task related to procurement requirements."
            ),
        )

    # ─────────────────────────────────────────────────────────
    # MAIN ENTRY POINT
    # ─────────────────────────────────────────────────────────
    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:

        logger.info("RequirementsSubAgent invoked")

        # ── Collect input text ──
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
                    "I understood you want to get a record, but I could not find a "
                    "record ID in your message. Please provide the 24-character record ID."
                )
            else:
                async for ev in self._handle_get(ctx, [record_id]):
                    yield ev

        elif intent == "delete":
            if not record_id:
                yield self._text_event(
                    "I understood you want to delete a record, but I could not find "
                    "a record ID in your message. Please provide the 24-character record ID."
                )
            else:
                async for ev in self._handle_delete(ctx, [record_id]):
                    yield ev

        elif intent == "update":
            if not record_id or not file_path:
                yield self._text_event(
                    "I understood you want to update a record, but I need both "
                    "a record ID and a new file path. Please provide both."
                )
            else:
                async for ev in self._handle_update(ctx, [record_id, file_path]):
                    yield ev

        elif intent == "upload":
            if not file_path:
                yield self._text_event(
                    "I understood you want to upload a file, but I could not find "
                    "a file path in your message. Please provide the full file path."
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
        """
        Use an LlmAgent to classify the user message into a structured
        intent dict. Falls back to {"intent": "qa"} on any failure.
        """
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

        logger.info("CRUD: list")
        try:
            records = requirements_collection.fetch_all()
        except Exception as e:
            yield self._text_event(f"Database error: {e}")
            return

        if not records:
            data = {"status": "empty", "count": 0, "records": []}
            yield self._text_event(
                _structured_response(
                    "📭 No requirement records found in the database.",
                    data,
                )
            )
            return

        summary = [
            {
                "id":         rec.get("_id"),
                "file_name":  rec.get("file_name"),
                "stored_path": rec.get("stored_path"),
                "length_chars": len(rec.get("raw_text", "")),
                "created_at": str(rec.get("created_at", "")),
            }
            for rec in records
        ]

        lines = [f"📋 {len(records)} record(s) in the database:\n"]
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
            yield self._text_event("Please provide a record ID.")
            return

        record_id = args[0].strip()
        logger.info(f"CRUD: get {record_id}")

        try:
            rec = requirements_collection.fetch_by_id(record_id)
        except Exception as e:
            yield self._text_event(f"Database error: {e}")
            return

        if not rec:
            yield self._text_event(
                _structured_response(
                    f"No record found with ID: {record_id}",
                    {"status": "not_found", "record_id": record_id},
                )
            )
            return

        structured = rec.get("data", {})
        structured_str = json.dumps(structured, indent=2)[:3000]

        human_msg = (
            f"📄 Record Details\n{'='*50}\n"
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
                    "status":     "ok",
                    "record_id":  record_id,
                    "file_name":  rec.get("file_name"),
                    "stored_path": rec.get("stored_path"),
                    "created_at": str(rec.get("created_at", "")),
                    "data":       structured,
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
            yield self._text_event("Please provide a record ID to delete.")
            return

        record_id = args[0].strip()
        logger.info(f"CRUD: delete {record_id}")

        try:
            rec = requirements_collection.fetch_by_id(record_id)
        except Exception as e:
            yield self._text_event(f"Database error: {e}")
            return

        if not rec:
            yield self._text_event(
                _structured_response(
                    f"No record found with ID: {record_id}",
                    {"status": "not_found", "record_id": record_id},
                )
            )
            return

        stored_path = rec.get("stored_path", "")
        file_name   = rec.get("file_name", "unknown")

        # ── Delete physical file ──
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

        # ── Delete DB record ──
        try:
            requirements_collection.delete_by_id(record_id)
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

        # ── Remove from session state if present ──
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
                "Please provide both a record ID and a new file path to update."
            )
            return

        record_id     = args[0].strip()
        new_file_path = args[1].strip()
        logger.info(f"CRUD: update {record_id} ← {new_file_path}")

        if not _is_file_path(new_file_path):
            yield self._text_event(
                f"File not found or unsupported format: {new_file_path}\n"
                "Supported: .docx, .pdf, .txt, .md"
            )
            return

        try:
            rec = requirements_collection.fetch_by_id(record_id)
        except Exception as e:
            yield self._text_event(f"Database error: {e}")
            return

        if not rec:
            yield self._text_event(
                _structured_response(
                    f"No record found with ID: {record_id}",
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
You are an intelligent document parser.
Extract ALL meaningful structured information from the document.
Rules:
- Do NOT use a fixed schema
- Create dynamic JSON based on content
- Preserve hierarchy
- Extract entities, metadata, tables, lists, and key-value pairs
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
            requirements_collection.update_by_id(record_id, update_payload)
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

        logger.info(f"Handling file upload: {file_path}")

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
You are an intelligent document parser.
Extract ALL meaningful structured information from the document.
Rules:
- Do NOT use a fixed schema
- Create dynamic JSON based on content
- Preserve hierarchy
- Extract entities, metadata, tables, lists, and key-value pairs
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
            inserted_id = requirements_collection.insert_one(record)
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
            f"✅ Document uploaded successfully.\n\n"
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

        logger.info(f"Handling question: {question}")

        # ── Load from session, fall back to DB ──
        existing_docs_raw = ctx.session.state.get("documents", "{}")
        try:
            documents = json.loads(existing_docs_raw)
        except Exception:
            documents = {}

        state_delta = {}
        if not documents:
            logger.info("Session empty — fetching all records from MongoDB for Q&A.")
            try:
                records = requirements_collection.fetch_all()
            except Exception as e:
                yield self._text_event(f"Database error while loading documents: {e}")
                return

            if not records:
                yield self._text_event(
                    _structured_response(
                        "No requirement records found in the database.",
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

            logger.info(f"Loaded {len(documents)} document(s) from DB into session.")
            state_delta["documents"] = json.dumps(documents)

        doc_context = _build_document_context(documents)
        doc_names   = list(documents.keys())

        qa_agent = LlmAgent(
            name="qa_agent",
            model=MODEL,
            instruction=f"""
You are an expert Requirements Analyst assistant with deep knowledge of procurement.

You have access to {len(documents)} requirement document(s):
{chr(10).join([f"  - {n}" for n in doc_names])}

You can answer ANY natural language question, including:
- Lookup & detail    : "What are the technical requirements for the laptop?"
- Filtering          : "List all requirements related to home appliances."
- Counting           : "How many requirements are for electronics items?"
- Quantities         : "How many units of laptops do we need and in which requirements?"
- Cross-doc analysis : "What is the ideal specification of all printer requirements?"

GUIDELINES:
- Always cite WHICH document your answer comes from.
- For counting/aggregation, scan ALL documents and sum where relevant.
- For filtering, return only matching documents.
- Present answers in clean structured format (bullet points or tables where helpful).
- If the answer is not in any document, say so clearly.

--- DOCUMENTS CONTEXT ---
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
requirements_sub_agent = RequirementsSubAgent()

__all__ = ["requirements_sub_agent"]
