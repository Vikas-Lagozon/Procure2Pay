# app.py
# ─────────────────────────────────────────────────────────────
# Procure2Pay — FastAPI Application
#
# REST Endpoints:
#   POST   /upload/requirement          — ingest requirement file
#   POST   /upload/vendor               — ingest vendor file
#   POST   /match/{requirement_id}      — run vendor matching
#   GET    /results/{requirement_id}    — fetch stored match results
#   GET    /vendors                     — list all vendors in DB
#   GET    /requirements                — list all requirements in DB
#   GET    /chat/stream                 — SSE streaming chatbot
#   POST   /new_chat                    — create new chat session
#   PATCH  /session/{id}/rename         — rename session
#   DELETE /session/{id}                — delete session
#   GET    /session/{id}/history        — fetch session history
#   GET    /health                      — health check
#   GET    /                            — UI home page
# ─────────────────────────────────────────────────────────────

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from chatbot import APP_NAME, USER_ID, chat_stream, runner, session_service
from nosql_db import MongoCollection
from requirements import upload_requirement_file
from vendors import upload_vendor_file
from matcher import match_vendors_for_requirement, fetch_results_for_requirement

logger = logging.getLogger(__name__)

# ── MongoDB collections for direct REST reads ─────────────────
_req_col     = MongoCollection("requirements")
_vendor_col  = MongoCollection("vendors")
_results_col = MongoCollection("matched_results")

# ── Temp directory for multipart uploads ──────────────────────
_TMP_DIR = Path("tmp_uploads")
_TMP_DIR.mkdir(parents=True, exist_ok=True)

# ── In-memory session metadata ────────────────────────────────
session_meta: dict = {}


# ─────────────────────────────────────────────────────────────
# Lifespan
# ─────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Procure2Pay starting up …")
    yield
    logger.info("Procure2Pay shutting down …")
    try:
        if runner and hasattr(runner, "shutdown"):
            await asyncio.wait_for(runner.shutdown(), timeout=5.0)
    except (asyncio.TimeoutError, Exception) as exc:
        logger.warning(f"Runner shutdown warning: {exc}")
    logger.info("Shutdown complete.")


# ─────────────────────────────────────────────────────────────
# FastAPI App
# ─────────────────────────────────────────────────────────────

app = FastAPI(title="Procure2Pay — Jarvis", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Static files and templates (optional — skip if not using UI)
_static_dir    = Path("static")
_templates_dir = Path("templates")

if _static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")

templates = Jinja2Templates(directory=str(_templates_dir)) if _templates_dir.exists() else None


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────

def _session_timestamp(session) -> str:
    for attr in ("create_time", "last_update_time"):
        val = getattr(session, attr, None)
        if val is None:
            continue
        if hasattr(val, "isoformat"):
            return val.isoformat()
        try:
            num = float(val)
            if num > 1e10:
                num /= 1000
            return datetime.fromtimestamp(num, tz=timezone.utc).isoformat()
        except (TypeError, ValueError):
            pass
        if isinstance(val, str) and val:
            return val
    return datetime.now(timezone.utc).isoformat()


async def _save_tmp_upload(upload: UploadFile) -> Path:
    """Save a multipart-uploaded file to a temp path and return it."""
    suffix = Path(upload.filename or "file").suffix.lower()
    tmp_path = _TMP_DIR / f"{uuid.uuid4().hex}{suffix}"
    try:
        with open(tmp_path, "wb") as fh:
            shutil.copyfileobj(upload.file, fh)
    finally:
        await upload.close()
    return tmp_path


# ─────────────────────────────────────────────────────────────
# STEP 1 — Requirement Upload
# ─────────────────────────────────────────────────────────────

@app.post("/upload/requirement", summary="Upload a requirement document (PDF / DOCX)")
async def upload_requirement_endpoint(file: UploadFile = File(...)):
    """
    Accepts a multipart PDF or DOCX file, runs LLM extraction,
    persists to MongoDB, and returns the requirement_id.
    """
    tmp_path = await _save_tmp_upload(file)
    try:
        result = upload_requirement_file(str(tmp_path))
    finally:
        tmp_path.unlink(missing_ok=True)

    if not result.success:
        raise HTTPException(status_code=422, detail=result.message)

    return JSONResponse({
        "success":          True,
        "requirement_id":   result.requirement_id,
        "message":          result.message,
        "extracted_fields": {
            "title":             result.extracted_data.get("title"),
            "budget":            result.extracted_data.get("budget"),
            "timeline":          result.extracted_data.get("timeline"),
            "location":          result.extracted_data.get("location"),
            "required_services": result.extracted_data.get("required_services", []),
            "description":       result.extracted_data.get("description", "")[:200],
        },
    })


# ─────────────────────────────────────────────────────────────
# STEP 2 — Vendor Upload / Fetch
# ─────────────────────────────────────────────────────────────

@app.post("/upload/vendor", summary="Upload a vendor capability document (PDF / DOCX)")
async def upload_vendor_endpoint(file: UploadFile = File(...)):
    """
    Accepts a multipart PDF or DOCX vendor profile, runs LLM extraction,
    persists to MongoDB, and returns the vendor_id.
    """
    tmp_path = await _save_tmp_upload(file)
    try:
        result = upload_vendor_file(str(tmp_path))
    finally:
        tmp_path.unlink(missing_ok=True)

    if not result.success:
        raise HTTPException(status_code=422, detail=result.message)

    return JSONResponse({
        "success":   True,
        "vendor_id": result.vendor_id,
        "message":   result.message,
        "extracted_fields": {
            "vendor_name":     result.extracted_data.get("vendor_name"),
            "services":        result.extracted_data.get("services", []),
            "experience_years": result.extracted_data.get("experience_years"),
            "pricing_model":   result.extracted_data.get("pricing_model"),
            "location":        result.extracted_data.get("location"),
            "rating":          result.extracted_data.get("rating"),
        },
    })


@app.get("/vendors", summary="Fetch all vendors from MongoDB")
async def list_vendors():
    """Return all registered vendors (normalised fields only)."""
    vendors = _vendor_col.fetch_all(
        projection={
            "_id": 1, "vendor_name": 1, "services": 1,
            "experience_years": 1, "pricing_model": 1,
            "location": 1, "rating": 1, "upload_timestamp": 1,
        }
    )
    return JSONResponse({"count": len(vendors), "vendors": vendors})


@app.get("/requirements", summary="Fetch all requirements from MongoDB")
async def list_requirements():
    """Return all ingested requirements (normalised fields only)."""
    reqs = _req_col.fetch_all(
        projection={
            "_id": 1, "title": 1, "budget": 1, "timeline": 1,
            "location": 1, "required_services": 1, "upload_timestamp": 1,
        }
    )
    return JSONResponse({"count": len(reqs), "requirements": reqs})


# ─────────────────────────────────────────────────────────────
# STEP 3 — Vendor Matching
# ─────────────────────────────────────────────────────────────

@app.post("/match/{requirement_id}", summary="Run vendor matching for a requirement")
async def run_matching(requirement_id: str):
    """
    Score all vendors against the stored requirement and persist
    the top-5 results to the matched_results collection.
    """
    result = match_vendors_for_requirement(requirement_id)

    if not result.success:
        raise HTTPException(status_code=404, detail=result.message)

    return JSONResponse({
        "success":        True,
        "requirement_id": requirement_id,
        "message":        result.message,
        "top_vendors": [
            {
                "rank":           rank,
                "vendor_id":      vm.vendor_id,
                "vendor_name":    vm.vendor_name,
                "score":          vm.score,
                "reason":         vm.reason,
                "services":       vm.services,
                "experience_years": vm.experience_years,
                "pricing_model":  vm.pricing_model,
                "location":       vm.location,
                "rating":         vm.rating,
            }
            for rank, vm in enumerate(result.top_vendors, start=1)
        ],
    })


# ─────────────────────────────────────────────────────────────
# STEP 4 — Fetch & Display Results
# ─────────────────────────────────────────────────────────────

@app.get("/results/{requirement_id}", summary="Fetch stored match results for a requirement")
async def get_results(requirement_id: str):
    """
    Returns the previously stored top-5 vendor matches from
    the matched_results collection (does NOT re-run matching).
    """
    doc = fetch_results_for_requirement(requirement_id)

    if doc is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"No stored match results for requirement '{requirement_id}'. "
                "Run POST /match/{requirement_id} first."
            ),
        )

    return JSONResponse(doc)


@app.get("/results", summary="List all stored match results")
async def list_all_results():
    """Return a summary of all matched_results documents."""
    docs = _results_col.fetch_all(
        projection={"_id": 1, "requirement_id": 1, "matched_at": 1, "vendor_count": 1}
    )
    return JSONResponse({"count": len(docs), "results": docs})


# ─────────────────────────────────────────────────────────────
# Chat — SSE Streaming
# ─────────────────────────────────────────────────────────────

@app.get("/chat/stream", summary="SSE streaming chat endpoint")
async def chat_stream_endpoint(user_input: str, session_id: str):
    """
    Streams the agent's final response as Server-Sent Events.
    The agent can call upload_requirement, upload_vendor,
    match_vendors, and fetch_results tools.
    """
    # Auto-name session from first message
    if session_id in session_meta and session_meta[session_id].get("name") == "New Chat":
        truncated = user_input[:30].strip()
        session_meta[session_id]["name"] = truncated + ("…" if len(user_input) > 30 else "")

    async def _event_gen():
        async for chunk in chat_stream(user_input, session_id):
            yield f"data: {json.dumps(chunk)}\n\n"

    return StreamingResponse(_event_gen(), media_type="text/event-stream")


# ─────────────────────────────────────────────────────────────
# Session Management
# ─────────────────────────────────────────────────────────────

@app.post("/new_chat", summary="Create a new chat session")
async def new_chat():
    new_session_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    await session_service.create_session(
        app_name=APP_NAME,
        user_id=USER_ID,
        session_id=new_session_id,
    )
    session_meta[new_session_id] = {"name": "New Chat", "created_at": now}
    return JSONResponse({"session_id": new_session_id, "name": "New Chat", "created_at": now})


@app.patch("/session/{session_id}/rename", summary="Rename a session")
async def rename_session(session_id: str, request: Request):
    body = await request.json()
    new_name = (body.get("name") or "").strip()
    if not new_name:
        return JSONResponse({"error": "Name cannot be empty."}, status_code=400)
    session_meta.setdefault(session_id, {})["name"] = new_name
    return JSONResponse({"session_id": session_id, "name": new_name})


@app.delete("/session/{session_id}", summary="Delete a session")
async def delete_session(session_id: str):
    try:
        await session_service.delete_session(
            app_name=APP_NAME,
            user_id=USER_ID,
            session_id=session_id,
        )
    except Exception:
        pass
    session_meta.pop(session_id, None)
    return JSONResponse({"deleted": session_id})


@app.get("/session/{session_id}/history", summary="Fetch session chat history")
async def get_session_history(session_id: str):
    try:
        session = await session_service.get_session(
            app_name=APP_NAME,
            user_id=USER_ID,
            session_id=session_id,
        )
    except Exception:
        return JSONResponse({"messages": []})

    if not session:
        return JSONResponse({"messages": []})

    messages = []
    for event in getattr(session, "events", None) or []:
        content = getattr(event, "content", None)
        if not content:
            continue
        parts = getattr(content, "parts", None) or []
        text_parts = [p.text for p in parts if getattr(p, "text", None)]
        if not text_parts:
            continue
        text = "".join(text_parts)
        role = getattr(content, "role", None)
        if role == "user":
            messages.append({"role": "user", "text": text})
        elif not getattr(event, "partial", False):
            messages.append({"role": "bot", "text": text})

    return JSONResponse({"messages": messages})


# ─────────────────────────────────────────────────────────────
# Home Page
# ─────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse, summary="UI home page")
async def index(request: Request):
    if templates is None:
        return HTMLResponse("<h1>Procure2Pay — Jarvis API</h1><p>No UI template found.</p>")

    sessions_response = await session_service.list_sessions(
        app_name=APP_NAME,
        user_id=USER_ID,
    )
    sessions = []
    if sessions_response and sessions_response.sessions:
        for s in sessions_response.sessions:
            sid = s.id
            meta = session_meta.get(sid, {})
            created_at = meta.get("created_at") or _session_timestamp(s)
            session_meta.setdefault(sid, {})["created_at"] = created_at
            sessions.append({
                "id":         sid,
                "name":       meta.get("name", sid[:8] + "…"),
                "created_at": created_at,
            })
        sessions.sort(key=lambda x: x["created_at"], reverse=True)

    return templates.TemplateResponse("index.html", {"request": request, "sessions": sessions})


# ─────────────────────────────────────────────────────────────
# Health Check
# ─────────────────────────────────────────────────────────────

@app.get("/health", summary="System health check")
async def health_check():
    """
    Checks MongoDB connectivity and session service availability.
    Response shape:
    {
        "status":          "healthy" | "degraded" | "unhealthy",
        "timestamp":       "<ISO-8601>",
        "mongodb":         "ok" | "error: ...",
        "session_service": "ok" | "error: ...",
        "active_sessions": <int>,
        "collections": {
            "requirements":    <int>,
            "vendors":         <int>,
            "matched_results": <int>
        }
    }
    """
    timestamp = datetime.now(timezone.utc).isoformat()

    # ── MongoDB health ────────────────────────────────────────
    mongo_status = "ok"
    collection_counts: dict = {}
    try:
        collection_counts = {
            "requirements":    _req_col.count(),
            "vendors":         _vendor_col.count(),
            "matched_results": _results_col.count(),
        }
    except Exception as exc:
        mongo_status = f"error: {exc}"

    # ── Session service health ────────────────────────────────
    session_status = "ok"
    active_sessions = 0
    try:
        result = await asyncio.wait_for(
            session_service.list_sessions(app_name=APP_NAME, user_id=USER_ID),
            timeout=5.0,
        )
        active_sessions = len(result.sessions) if result and result.sessions else 0
    except asyncio.TimeoutError:
        session_status = "timeout"
    except Exception as exc:
        session_status = f"error: {exc}"

    # ── Overall status ────────────────────────────────────────
    all_ok = mongo_status == "ok" and session_status == "ok"
    any_ok = mongo_status == "ok" or session_status == "ok"
    overall = "healthy" if all_ok else ("degraded" if any_ok else "unhealthy")

    return JSONResponse(
        status_code=200 if overall == "healthy" else 207,
        content={
            "status":          overall,
            "timestamp":       timestamp,
            "mongodb":         mongo_status,
            "session_service": session_status,
            "active_sessions": active_sessions,
            "collections":     collection_counts,
        },
    )


# ─────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────
# Run with:
#   uvicorn app:app --host 0.0.0.0 --port 8000 --reload