# requirements_agent/tools.py

import sys
import json
import datetime
from pathlib import Path
from typing import AsyncGenerator

# ── Ensure both the agent dir and root dir are on sys.path ──────────────────
_AGENT_DIR = Path(__file__).resolve().parent
_ROOT_DIR  = _AGENT_DIR.parent
for _p in (_AGENT_DIR, _ROOT_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
# ─────────────────────────────────────────────────────────────────────────────

from google.adk.agents import LlmAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event, EventActions
from google.genai import types

from requirements_agent.config import config
from requirements_agent.nosql_db import MongoCollection
from requirements_agent.file_ops import save_file, delete_file, replace_file

from requirements_agent.utils import (
    extract_text,
    parse_json_safely,
    is_file_path,
    format_record,
    build_document_context,
)
from requirements_agent.prompts import requirement_qa_prompt, structurer_prompt, requirement_qa_prompt
from requirements_agent.logger import get_logger

logger = get_logger(__name__)

MODEL       = config.MODEL
STORAGE_DIR = _AGENT_DIR / "REQUIREMENTS"

requirements_collection = MongoCollection("requirements")


# ─────────────────────────────────────────────────────────────
# SHARED HELPERS
# ─────────────────────────────────────────────────────────────

def text_event(author: str, text: str) -> Event:
    return Event(
        author=author,
        content=types.Content(
            role="model",
            parts=[types.Part(text=text)],
        ),
    )


def _load_session_docs(ctx: InvocationContext) -> dict:
    raw = ctx.session.state.get("requirements_docs", "{}")
    try:
        return json.loads(raw)
    except Exception:
        return {}


def _save_session_docs(existing_docs: dict) -> EventActions:
    return EventActions(
        state_delta={"requirements_docs": json.dumps(existing_docs)}
    )


# ─────────────────────────────────────────────────────────────
# UPLOAD
# ─────────────────────────────────────────────────────────────

async def handle_upload(
    author: str, ctx: InvocationContext, file_path: str
) -> AsyncGenerator[Event, None]:

    logger.info(f"Upload: {file_path}")

    if not is_file_path(file_path):
        yield text_event(
            author,
            f" File not found or unsupported format: {file_path}\n"
            "Supported: .docx, .pdf, .txt, .md",
        )
        return

    try:
        stored_path = save_file(file_path, str(STORAGE_DIR))
    except Exception as e:
        yield text_event(author, f" File save failed: {e}")
        return

    try:
        text_content = extract_text(stored_path)
    except Exception as e:
        yield text_event(author, f" Text extraction failed: {e}")
        return

    if not text_content.strip():
        yield text_event(author, " File is empty or unreadable.")
        return

    logger.info(f"Extracted {len(text_content)} chars")

    struct_agent = LlmAgent(
        name="dynamic_structurer",
        model=MODEL,
        instruction=structurer_prompt(text_content),
    )
    structured_text = ""
    async for event in struct_agent.run_async(ctx):
        if event.is_final_response() and event.content:
            for part in event.content.parts:
                if getattr(part, "text", None):
                    structured_text += part.text

    structured_json = parse_json_safely(structured_text)
    logger.debug(f"Structured keys: {list(structured_json.keys())}")

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
        yield text_event(author, f" Database insert failed: {e}")
        return

    logger.info(f"Stored with ID: {inserted_id}")

    existing_docs = _load_session_docs(ctx)
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
        author=author,
        content=types.Content(
            role="model",
            parts=[types.Part(text=(
                f"✅ Document uploaded successfully.\n\n"
                f"File      : {original_name}\n"
                f"Record ID : {inserted_id}\n"
                f"Length    : {len(text_content)} characters\n\n"
                f"📂 Documents in this session ({len(existing_docs)}):\n"
                f"{doc_list}\n\n"
                f"You can now ask questions about any of these documents."
            ))],
        ),
        actions=EventActions(
            state_delta={
                "requirements_docs": json.dumps(existing_docs),
                "last_file_name":    original_name,
            }
        ),
    )


# ─────────────────────────────────────────────────────────────
# LIST
# ─────────────────────────────────────────────────────────────

async def handle_list(
    author: str, ctx: InvocationContext
) -> AsyncGenerator[Event, None]:

    logger.info("CRUD: list")
    try:
        records = requirements_collection.fetch_all()
    except Exception as e:
        yield text_event(author, f" Database error: {e}")
        return

    if not records:
        yield text_event(
            author,
            " No requirement records found in the database.\n"
            'Upload a document — just say "Upload <file_path>".',
        )
        return

    lines = [f"📋 {len(records)} record(s) in the database:\n"]
    for i, rec in enumerate(records, 1):
        lines.append(f"{'─'*50}")
        lines.append(f"[{i}]")
        lines.append(format_record(rec))

    yield text_event(author, "\n".join(lines))


# ─────────────────────────────────────────────────────────────
# GET  (one or many records)
# ─────────────────────────────────────────────────────────────

async def handle_get(
    author: str, ctx: InvocationContext, record_ids: list | str
) -> AsyncGenerator[Event, None]:
    """record_ids: list of ID strings  OR  the string "all"."""

    logger.info(f"CRUD: get | targets={record_ids}")

    if record_ids == "all":
        async for ev in handle_list(author, ctx):
            yield ev
        return

    if not record_ids:
        yield text_event(author, "Please specify which record(s) to view.")
        return

    for record_id in record_ids:
        try:
            rec = requirements_collection.fetch_by_id(record_id)
        except Exception as e:
            yield text_event(author, f" DB error for {record_id}: {e}")
            continue

        if not rec:
            yield text_event(author, f"  No record found with ID: {record_id}")
            continue

        structured_str = json.dumps(rec.get("data", {}), indent=2)[:3000]
        msg = (
            f"📄 Record Details\n{'='*50}\n"
            f"{format_record(rec)}\n"
            f"--- Structured Data (truncated to 3000 chars) ---\n"
            f"{structured_str}\n\n"
            f"--- Raw Text Preview (first 500 chars) ---\n"
            f"{rec.get('raw_text', '')[:500]}"
        )
        yield text_event(author, msg)


# ─────────────────────────────────────────────────────────────
# DELETE  (one, several, or all records)
# ─────────────────────────────────────────────────────────────

async def handle_delete(
    author: str, ctx: InvocationContext, record_ids: list | str
) -> AsyncGenerator[Event, None]:
    """record_ids: list of ID strings  OR  the string "all"."""

    logger.info(f"CRUD: delete | targets={record_ids}")

    # Resolve "all" → every record in DB
    if record_ids == "all":
        try:
            all_records = requirements_collection.fetch_all()
        except Exception as e:
            yield text_event(author, f" Database error: {e}")
            return

        if not all_records:
            yield text_event(author, " No records to delete.")
            return

        record_ids = [str(rec["_id"]) for rec in all_records]
        logger.info(f"Resolved 'all' → {len(record_ids)} record(s)")

    if not record_ids:
        yield text_event(author, "  No matching records found to delete.")
        return

    existing_docs = _load_session_docs(ctx)
    results       = []

    for record_id in record_ids:

        try:
            rec = requirements_collection.fetch_by_id(record_id)
        except Exception as e:
            results.append(f" DB fetch error for {record_id}: {e}")
            continue

        if not rec:
            results.append(f"Not found: {record_id}")
            continue

        stored_path = rec.get("stored_path", "")
        file_name   = rec.get("file_name", "unknown")

        # Delete physical file
        file_msg = ""
        if stored_path:
            try:
                deleted  = delete_file(stored_path, str(STORAGE_DIR))
                file_msg = (
                    f"🗑  File deleted: {stored_path}"
                    if deleted
                    else f"  File not on disk: {stored_path}"
                )
            except ValueError as ve:
                file_msg = f"  File skip: {ve}"
            except Exception as e:
                file_msg = f"  File delete error: {e}"
        else:
            file_msg = "  No stored_path — file deletion skipped."

        # Delete DB record
        try:
            requirements_collection.delete_by_id(record_id)
            db_msg = f"✅ DB record deleted: {record_id}"
        except Exception as e:
            results.append(
                f" {file_name}: file={file_msg} | DB delete failed: {e}"
            )
            continue

        # Remove from session
        removed_key = None
        for key, val in list(existing_docs.items()):
            if val.get("record_id") == record_id:
                removed_key = key
                del existing_docs[key]
                break

        session_msg = (
            f"🗂  Session removed: {removed_key}"
            if removed_key
            else "ℹ️  Not in current session."
        )

        results.append(
            f"── {file_name}\n   {db_msg}\n   {file_msg}\n   {session_msg}"
        )
        logger.info(f"Deleted: {file_name} ({record_id})")

    summary = (
        f"🗑  Delete complete — {len(record_ids)} record(s) processed.\n\n"
        + "\n\n".join(results)
    )

    yield Event(
        author=author,
        content=types.Content(
            role="model",
            parts=[types.Part(text=summary)],
        ),
        actions=_save_session_docs(existing_docs),
    )


# ─────────────────────────────────────────────────────────────
# UPDATE
# ─────────────────────────────────────────────────────────────

async def handle_update(
    author: str, ctx: InvocationContext, record_id: str, new_file_path: str
) -> AsyncGenerator[Event, None]:

    logger.info(f"CRUD: update | {record_id} ← {new_file_path}")

    if not is_file_path(new_file_path):
        yield text_event(
            author,
            f" File not found or unsupported format: {new_file_path}\n"
            "Supported: .docx, .pdf, .txt, .md",
        )
        return

    try:
        rec = requirements_collection.fetch_by_id(record_id)
    except Exception as e:
        yield text_event(author, f" Database error: {e}")
        return

    if not rec:
        yield text_event(author, f"  No record found with ID: {record_id}")
        return

    stored_path   = rec.get("stored_path", "")
    old_file_name = rec.get("file_name", "unknown")

    try:
        replace_file(stored_path, new_file_path, str(STORAGE_DIR))
        file_msg = f"✅ File replaced: {stored_path}"
    except Exception as e:
        yield text_event(author, f" File replacement failed: {e}")
        return

    try:
        new_text = extract_text(stored_path)
    except Exception as e:
        yield text_event(author, f" Text extraction failed: {e}")
        return

    if not new_text.strip():
        yield text_event(author, " New file appears to be empty or unreadable.")
        return

    struct_agent = LlmAgent(
        name="dynamic_structurer",
        model=MODEL,
        instruction=structurer_prompt(new_text),
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
        db_msg = f"✅ DB record updated: {record_id}"
    except Exception as e:
        yield text_event(
            author,
            f"{file_msg}\n DB update failed: {e}",
        )
        return

    existing_docs = _load_session_docs(ctx)
    session_key = None
    for key, val in existing_docs.items():
        if val.get("record_id") == record_id:
            session_key = key
            existing_docs[key]["raw_text"]        = new_text
            existing_docs[key]["structured_json"] = json.dumps(structured_json)
            existing_docs[key]["uploaded_at"]     = datetime.datetime.utcnow().isoformat()
            break

    session_msg = (
        f"🗂  Session refreshed: {session_key}"
        if session_key
        else "ℹ️  Not in current session — session unchanged."
    )

    yield Event(
        author=author,
        content=types.Content(
            role="model",
            parts=[types.Part(text=(
                f"🔄 Update complete for: {old_file_name}\n\n"
                f"{file_msg}\n"
                f"{db_msg}\n"
                f"{session_msg}\n\n"
                f"New length     : {len(new_text)} characters\n"
                f"Structured keys: {list(structured_json.keys())}"
            ))],
        ),
        actions=_save_session_docs(existing_docs),
    )


# ─────────────────────────────────────────────────────────────
# Q&A
# ─────────────────────────────────────────────────────────────

async def handle_question(
    author: str, ctx: InvocationContext, question: str
) -> AsyncGenerator[Event, None]:

    logger.info(f"Q&A: {question}")

    documents   = _load_session_docs(ctx)
    state_delta = {}

    if not documents:
        logger.info("Session empty — fetching all records from MongoDB.")
        try:
            records = requirements_collection.fetch_all()
        except Exception as e:
            yield text_event(author, f" Database error: {e}")
            return

        if not records:
            yield text_event(
                author,
                " No requirement records found.\n"
                'Upload a document first — say "Upload <file_path>".',
            )
            return

        for rec in records:
            name = rec.get("file_name", str(rec.get("_id", "unknown")))
            documents[name] = {
                "file_name":       rec.get("file_name", ""),
                "stored_path":     rec.get("stored_path", ""),
                "record_id":       str(rec.get("_id", "")),
                "raw_text":        rec.get("raw_text", ""),
                "structured_json": json.dumps(rec.get("data", {})),
                "uploaded_at":     str(rec.get("created_at", "")),
            }

        logger.info(f"Loaded {len(documents)} document(s) from DB.")
        state_delta["requirements_docs"] = json.dumps(documents)

    doc_context = build_document_context(documents)
    qa_agent    = LlmAgent(
        name="qa_agent",
        model=MODEL,
        instruction=requirement_qa_prompt(documents, doc_context),
    )

    if state_delta:
        yield Event(
            author=author,
            content=types.Content(role="model", parts=[types.Part(text="")]),
            actions=EventActions(state_delta=state_delta),
        )

    async for event in qa_agent.run_async(ctx):
        if event.is_final_response() and event.content:
            yield event