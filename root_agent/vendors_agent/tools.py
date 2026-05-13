# vendors_agent/tools.py
"""
Heavy business-logic operations for the Vendors Agent.

Every public function is an async generator that yields
google.adk.events.Event objects. The VendorsChatbot agent in agent.py
delegates all work here — agent.py stays a clean orchestrator.

Exports
-------
handle_upload(ctx, file_path, author)
handle_list(ctx, author)
handle_get(ctx, record_ids, author)          ← accepts list | "all"
handle_delete(ctx, record_ids, author)       ← accepts list | "all"
handle_update(ctx, record_id, new_file_path, author)
handle_question(ctx, question, author)
make_text_event(author, text)
"""

from __future__ import annotations

import sys
import datetime
import json
from pathlib import Path
from typing import AsyncGenerator

# ── Ensure both the agent dir and root dir are on sys.path ──────────────────
_AGENT_DIR = Path(__file__).resolve().parent   # vendors_agent/
_ROOT_DIR  = _AGENT_DIR.parent                 # root_agent/
for _p in (_AGENT_DIR, _ROOT_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
# ─────────────────────────────────────────────────────────────────────────────

from google.adk.agents import LlmAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event, EventActions
from google.genai import types

from vendors_agent.config import config
from vendors_agent.file_ops import delete_file, replace_file, save_file
from vendors_agent.logger import get_logger
from vendors_agent.nosql_db import MongoCollection
from vendors_agent.prompts import (
    delete_success_message,
    structuring_prompt,
    update_success_message,
    upload_success_message,
    vendor_qa_prompt,
)
from vendors_agent.utils import (
    build_document_context,
    extract_text,
    format_record,
    load_documents_from_state,
    parse_json_safely,
    session_entry_from_record,
    validate_file_for_upload,
)

logger = get_logger(__name__)

MODEL       = config.MODEL
STORAGE_DIR = _AGENT_DIR / "VENDORS"   # always absolute, CWD-independent

vendors_collection = MongoCollection("vendors")


# ─────────────────────────────────────────────────────────────
# CONVENIENCE FACTORY
# ─────────────────────────────────────────────────────────────

def make_text_event(author: str, text: str) -> Event:
    """Return a plain-text model Event without state changes."""
    return Event(
        author=author,
        content=types.Content(
            role="model",
            parts=[types.Part(text=text)],
        ),
    )


# ─────────────────────────────────────────────────────────────
# INTERNAL — dynamic structurer LLM
# ─────────────────────────────────────────────────────────────

async def _run_structurer(ctx: InvocationContext, text_content: str) -> dict:
    """Spin up a short-lived LlmAgent to extract structured JSON from raw text."""
    struct_agent = LlmAgent(
        name="dynamic_structurer",
        model=MODEL,
        instruction=structuring_prompt(text_content),
    )
    raw_output = ""
    async for event in struct_agent.run_async(ctx):
        if event.is_final_response() and event.content:
            for part in event.content.parts:
                if getattr(part, "text", None):
                    raw_output += part.text
    return parse_json_safely(raw_output)


# ─────────────────────────────────────────────────────────────
# INTERNAL — session helpers
# ─────────────────────────────────────────────────────────────

def _get_session_docs(ctx: InvocationContext) -> dict:
    raw = ctx.session.state.get("vendors_docs", "{}")
    return load_documents_from_state(raw)


def _state_delta_for_docs(documents: dict) -> dict:
    return {"vendors_docs": json.dumps(documents)}


# ─────────────────────────────────────────────────────────────
# UPLOAD
# ─────────────────────────────────────────────────────────────

async def handle_upload(
    ctx: InvocationContext,
    file_path: str,
    author: str,
) -> AsyncGenerator[Event, None]:
    """Save vendor document, extract text, structure with LLM, insert to MongoDB."""
    logger.info(f"[tools] handle_upload: {file_path}")

    err = validate_file_for_upload(file_path)
    if err:
        yield make_text_event(author, f"❌ {err}")
        return

    try:
        stored_path = save_file(file_path, str(STORAGE_DIR))
    except Exception as e:
        yield make_text_event(author, f"❌ File save failed: {e}")
        return

    try:
        text_content = extract_text(stored_path)
    except Exception as e:
        yield make_text_event(author, f"❌ Text extraction failed: {e}")
        return

    if not text_content.strip():
        yield make_text_event(author, "❌ File is empty or unreadable.")
        return

    logger.info(f"[tools] Extracted {len(text_content)} chars")

    structured_json = await _run_structurer(ctx, text_content)
    logger.debug(f"[tools] Structured keys: {list(structured_json.keys())}")

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
        yield make_text_event(author, f"❌ Database insert failed: {e}")
        return

    logger.info(f"[tools] Stored vendor with ID: {inserted_id}")

    existing_docs = _get_session_docs(ctx)
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
        f"  {i+1}. {name}" for i, name in enumerate(existing_docs.keys())
    )

    yield Event(
        author=author,
        content=types.Content(
            role="model",
            parts=[types.Part(text=upload_success_message(
                original_name=original_name,
                inserted_id=str(inserted_id),
                text_length=len(text_content),
                num_docs_in_session=len(existing_docs),
                doc_list=doc_list,
            ))],
        ),
        actions=EventActions(
            state_delta={
                **_state_delta_for_docs(existing_docs),
                "last_file_name": original_name,
            }
        ),
    )


# ─────────────────────────────────────────────────────────────
# LIST
# ─────────────────────────────────────────────────────────────

async def handle_list(
    ctx: InvocationContext,
    author: str,
) -> AsyncGenerator[Event, None]:
    """Fetch and display all vendor records from MongoDB."""
    logger.info("[tools] CRUD: list")

    try:
        records = vendors_collection.fetch_all()
    except Exception as e:
        yield make_text_event(author, f"❌ Database error: {e}")
        return

    if not records:
        yield make_text_event(
            author,
            "📭 No vendor records found in the database.\n"
            'Upload a vendor document — just say "Upload <file_path>".',
        )
        return

    lines = [f"📋 {len(records)} vendor record(s) in the database:\n"]
    for i, rec in enumerate(records, 1):
        lines.append(f"{'─'*50}")
        lines.append(f"[{i}]")
        lines.append(format_record(rec))

    yield make_text_event(author, "\n".join(lines))


# ─────────────────────────────────────────────────────────────
# GET  (one or many records)
# ─────────────────────────────────────────────────────────────

async def handle_get(
    ctx: InvocationContext,
    record_ids: list[str] | str,
    author: str,
) -> AsyncGenerator[Event, None]:
    """
    Fetch and display one or more vendor records.

    record_ids: list of ID strings  OR  the string "all"
    """
    logger.info(f"[tools] CRUD: get | targets={record_ids}")

    if record_ids == "all":
        async for ev in handle_list(ctx, author):
            yield ev
        return

    if not record_ids:
        yield make_text_event(author, "Please specify which vendor record(s) to view.")
        return

    for record_id in record_ids:
        try:
            rec = vendors_collection.fetch_by_id(record_id)
        except Exception as e:
            yield make_text_event(author, f"❌ DB error for {record_id}: {e}")
            continue

        if not rec:
            yield make_text_event(author, f"⚠️  No vendor record found with ID: {record_id}")
            continue

        structured_str = json.dumps(rec.get("data", {}), indent=2)[:3000]
        msg = (
            f"📄 Vendor Record Details\n{'='*50}\n"
            f"{format_record(rec)}\n"
            f"--- Structured Data (truncated to 3 000 chars) ---\n"
            f"{structured_str}\n\n"
            f"--- Raw Text Preview (first 500 chars) ---\n"
            f"{rec.get('raw_text', '')[:500]}"
        )
        yield make_text_event(author, msg)


# ─────────────────────────────────────────────────────────────
# DELETE  (one, several, or all records)
# ─────────────────────────────────────────────────────────────

async def handle_delete(
    ctx: InvocationContext,
    record_ids: list[str] | str,
    author: str,
) -> AsyncGenerator[Event, None]:
    """
    Delete one, several, or all vendor records.

    record_ids: list of ID strings  OR  the string "all"
    """
    logger.info(f"[tools] CRUD: delete | targets={record_ids}")

    # Resolve "all" → every record in DB
    if record_ids == "all":
        try:
            all_records = vendors_collection.fetch_all()
        except Exception as e:
            yield make_text_event(author, f"❌ Database error: {e}")
            return

        if not all_records:
            yield make_text_event(author, "📭 No vendor records to delete.")
            return

        record_ids = [str(rec["_id"]) for rec in all_records]
        logger.info(f"[tools] Resolved 'all' → {len(record_ids)} record(s)")

    if not record_ids:
        yield make_text_event(author, "⚠️  No matching vendor records found to delete.")
        return

    existing_docs = _get_session_docs(ctx)
    results: list[str] = []

    for record_id in record_ids:

        try:
            rec = vendors_collection.fetch_by_id(record_id)
        except Exception as e:
            results.append(f"❌ DB fetch error for {record_id}: {e}")
            continue

        if not rec:
            results.append(f"⚠️  Not found: {record_id}")
            continue

        stored_path = rec.get("stored_path", "")
        file_name   = rec.get("file_name", "unknown")

        # Delete physical file
        file_msg: str
        if stored_path:
            try:
                deleted  = delete_file(stored_path, str(STORAGE_DIR))
                file_msg = (
                    f"🗑  File deleted: {stored_path}"
                    if deleted
                    else f"⚠️  File not on disk: {stored_path}"
                )
            except ValueError as ve:
                file_msg = f"⚠️  File skip: {ve}"
            except Exception as e:
                file_msg = f"⚠️  File delete error: {e}"
        else:
            file_msg = "⚠️  No stored_path — file deletion skipped."

        # Delete DB record
        try:
            vendors_collection.delete_by_id(record_id)
            db_msg = f"✅ DB record deleted: {record_id}"
        except Exception as e:
            results.append(
                f"❌ {file_name}: file={file_msg} | DB delete failed: {e}"
            )
            continue

        # Remove from session
        removed_key: str | None = None
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
        logger.info(f"[tools] Deleted: {file_name} ({record_id})")

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
        actions=EventActions(state_delta=_state_delta_for_docs(existing_docs)),
    )


# ─────────────────────────────────────────────────────────────
# UPDATE
# ─────────────────────────────────────────────────────────────

async def handle_update(
    ctx: InvocationContext,
    record_id: str,
    new_file_path: str,
    author: str,
) -> AsyncGenerator[Event, None]:
    """
    Replace the stored vendor file, re-extract text, re-structure, update DB + session.
    """
    logger.info(f"[tools] CRUD: update | {record_id} ← {new_file_path}")

    err = validate_file_for_upload(new_file_path)
    if err:
        yield make_text_event(author, f"❌ {err}")
        return

    try:
        rec = vendors_collection.fetch_by_id(record_id)
    except Exception as e:
        yield make_text_event(author, f"❌ Database error: {e}")
        return

    if not rec:
        yield make_text_event(author, f"⚠️  No vendor record found with ID: {record_id}")
        return

    stored_path   = rec.get("stored_path", "")
    old_file_name = rec.get("file_name", "unknown")

    try:
        replace_file(stored_path, new_file_path, str(STORAGE_DIR))
        file_msg = f"✅ File replaced: {stored_path}"
    except Exception as e:
        yield make_text_event(author, f"❌ File replacement failed: {e}")
        return

    try:
        new_text = extract_text(stored_path)
    except Exception as e:
        yield make_text_event(author, f"❌ Text extraction failed: {e}")
        return

    if not new_text.strip():
        yield make_text_event(author, "❌ New file appears to be empty or unreadable.")
        return

    structured_json = await _run_structurer(ctx, new_text)

    update_payload = {
        "raw_text":   new_text,
        "data":       structured_json,
        "updated_at": datetime.datetime.utcnow(),
        "file_name":  Path(stored_path).name,
    }
    try:
        vendors_collection.update_by_id(record_id, update_payload)
        db_msg = f"✅ DB record updated: {record_id}"
    except Exception as e:
        yield make_text_event(
            author,
            f"{file_msg}\n❌ DB update failed: {e}",
        )
        return

    existing_docs = _get_session_docs(ctx)
    session_key: str | None = None
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
            parts=[types.Part(text=update_success_message(
                old_file_name=old_file_name,
                file_msg=file_msg,
                db_msg=db_msg,
                session_msg=session_msg,
                new_text_length=len(new_text),
                structured_keys=list(structured_json.keys()),
            ))],
        ),
        actions=EventActions(state_delta=_state_delta_for_docs(existing_docs)),
    )


# ─────────────────────────────────────────────────────────────
# Q&A
# ─────────────────────────────────────────────────────────────

async def handle_question(
    ctx: InvocationContext,
    question: str,
    author: str,
) -> AsyncGenerator[Event, None]:
    """Answer a natural-language question using vendor documents."""
    logger.info(f"[tools] handle_question: {question}")

    documents   = _get_session_docs(ctx)
    state_delta: dict = {}

    if not documents:
        logger.info("[tools] Session empty — fetching all vendor records from MongoDB.")
        try:
            records = vendors_collection.fetch_all()
        except Exception as e:
            yield make_text_event(author, f"❌ Database error: {e}")
            return

        if not records:
            yield make_text_event(
                author,
                "📭 No vendor records found in the database.\n"
                'Upload a vendor document first — say "Upload <file_path>".',
            )
            return

        for rec in records:
            key = rec.get("file_name") or str(rec.get("_id", "unknown"))
            documents[key] = session_entry_from_record(rec)

        logger.info(f"[tools] Loaded {len(documents)} vendor document(s) from DB.")
        state_delta["vendors_docs"] = json.dumps(documents)

    doc_context = build_document_context(documents)
    doc_names   = list(documents.keys())

    if state_delta:
        yield Event(
            author=author,
            content=types.Content(role="model", parts=[types.Part(text="")]),
            actions=EventActions(state_delta=state_delta),
        )

    qa_agent = LlmAgent(
        name="vendor_qa_agent",
        model=MODEL,
        instruction=vendor_qa_prompt(
            num_documents=len(documents),
            doc_names=doc_names,
            doc_context=doc_context,
        ),
    )

    async for event in qa_agent.run_async(ctx):
        if event.is_final_response() and event.content:
            yield event
