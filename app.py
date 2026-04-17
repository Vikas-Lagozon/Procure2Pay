# app.py
from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi import FastAPI, Request, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from chatbot import (
    chat_stream,
    session_service,
    APP_NAME,
    USER_ID,
    expense_tracker_mcp,
    to_do_mcp,
    file_system_mcp,
    runner,
)
import uuid
import json
import asyncio
import logging
import httpx
from datetime import datetime, timezone
from contextlib import asynccontextmanager

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────
# Lifespan (startup / shutdown)
# ─────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Jarvis starting up...")
    yield
    logger.info("Jarvis shutting down gracefully...")
    try:
        if runner and hasattr(runner, "shutdown"):
            await asyncio.wait_for(runner.shutdown(), timeout=5.0)
    except (asyncio.TimeoutError, Exception) as e:
        logger.warning(f"Runner shutdown warning (safe to ignore): {e}")
    # MCP subprocesses auto-terminate on process exit; no explicit close needed
    logger.info("Shutdown complete.")


app = FastAPI(title="Jarvis Chatbot", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Restrict in prod (e.g., ["https://yourdomain.com"])
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# In-memory session metadata store: { session_id: { name, created_at } }
session_meta: dict = {}


# ─────────────────────────────────────────
# Helper: extract a UTC ISO timestamp from an ADK session object
# ─────────────────────────────────────────
def _session_timestamp(s) -> str:
    for attr in ("create_time", "last_update_time"):
        val = getattr(s, attr, None)
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


# ─────────────────────────────────────────
# Helper: ping a single MCP toolset
# ─────────────────────────────────────────
async def _check_mcp(name: str, toolset) -> dict:
    """
    Try to fetch the tool list from an MCP toolset.
    Returns a status dict: { name, status, tools, error }
    """
    try:
        tools = await asyncio.wait_for(toolset.get_tools(None), timeout=5.0)
        tool_names = [getattr(t, "name", str(t)) for t in (tools or [])]
        return {
            "name":   name,
            "status": "ok",
            "tools":  tool_names,
            "error":  None,
        }
    except asyncio.TimeoutError:
        return {
            "name":   name,
            "status": "timeout",
            "tools":  [],
            "error":  "MCP server did not respond within 5 seconds.",
        }
    except Exception as e:
        return {
            "name":   name,
            "status": "error",
            "tools":  [],
            "error":  str(e),
        }


# ─────────────────────────────────────────
# Helper: ping the remote A2A agent card
# ─────────────────────────────────────────
async def _check_a2a_agent(name: str, agent) -> dict:
    """
    Ping the remote A2A agent's well-known agent card endpoint.
    Returns a status dict: { name, status, url, error }
    """
    agent_card_url = getattr(agent, "_agent_card_url", None) or getattr(agent, "agent_card", None)
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(agent_card_url)
            if response.status_code == 200:
                return {
                    "name":   name,
                    "status": "ok",
                    "url":    agent_card_url,
                    "error":  None,
                }
            else:
                return {
                    "name":   name,
                    "status": "error",
                    "url":    agent_card_url,
                    "error":  f"HTTP {response.status_code}",
                }
    except asyncio.TimeoutError:
        return {
            "name":   name,
            "status": "timeout",
            "url":    agent_card_url,
            "error":  "A2A agent did not respond within 5 seconds.",
        }
    except Exception as e:
        return {
            "name":   name,
            "status": "error",
            "url":    agent_card_url,
            "error":  str(e),
        }


# ─────────────────────────────────────────
# Health Check
# ─────────────────────────────────────────
@app.get("/health")
async def health_check():
    """
    Health check endpoint.
    Pings all MCP servers and remote A2A agents and reports their status.

    Response shape:
    {
        "status":    "healthy" | "degraded" | "unhealthy",
        "timestamp": "<ISO-8601>",
        "mcp_servers": [
            { "name": "web_reader",     "status": "ok" | "timeout" | "error", "tools": [...], "error": null },
            { "name": "expense_tracker","status": "ok" | "timeout" | "error", "tools": [...], "error": null },
            { "name": "to_do",          "status": "ok" | "timeout" | "error", "tools": [...], "error": null },
            { "name": "file_system",    "status": "ok" | "timeout" | "error", "tools": [...], "error": null },
        ],
        "a2a_agents": [
            { "name": "hello_world_agent", "status": "ok" | "timeout" | "error", "url": "...", "error": null },
        ],
        "session_service": "ok" | "error",
        "active_sessions": <int>
    }
    """
    timestamp = datetime.now(timezone.utc).isoformat()

    # ── 1. Ping all MCP servers concurrently ──────────────────────────────
    mcp_results = await asyncio.gather(
        _check_mcp("expense_tracker", expense_tracker_mcp),
        _check_mcp("to_do",           to_do_mcp),
        _check_mcp("file_system",     file_system_mcp),
        return_exceptions=False,
    )

    # ── 3. Ping session service ────────────────────────────────────────────
    session_service_status = "ok"
    active_sessions = 0
    try:
        result = await asyncio.wait_for(
            session_service.list_sessions(app_name=APP_NAME, user_id=USER_ID),
            timeout=5.0,
        )
        active_sessions = len(result.sessions) if result and result.sessions else 0
    except asyncio.TimeoutError:
        session_service_status = "timeout"
    except Exception as e:
        session_service_status = f"error: {e}"

    # ── 4. Overall status ─────────────────────────────────────────────────
    all_results = list(mcp_results)
    all_ok      = all(r["status"] == "ok" for r in all_results)
    any_ok      = any(r["status"] == "ok" for r in all_results)
    db_ok       = session_service_status == "ok"

    if all_ok and db_ok:
        overall = "healthy"
    elif any_ok or db_ok:
        overall = "degraded"
    else:
        overall = "unhealthy"

    return JSONResponse(
        status_code=200 if overall == "healthy" else 207,
        content={
            "status":          overall,
            "timestamp":       timestamp,
            "mcp_servers":     list(mcp_results),
            "session_service": session_service_status,
            "active_sessions": active_sessions,
        },
    )


# ─────────────────────────────────────────
# Home page
# ─────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    sessions_response = await session_service.list_sessions(
        app_name=APP_NAME,
        user_id=USER_ID,
    )
    sessions = []
    if sessions_response and sessions_response.sessions:
        for s in sessions_response.sessions:
            sid = s.id
            meta = session_meta.get(sid, {})
            created_at = meta.get("created_at", "")
            if not created_at:
                created_at = _session_timestamp(s)
                session_meta.setdefault(sid, {})["created_at"] = created_at
            sessions.append({
                "id":         sid,
                "name":       meta.get("name", sid[:8] + "…"),
                "created_at": created_at,
            })
        sessions.sort(key=lambda x: x["created_at"], reverse=True)

    return templates.TemplateResponse("index.html", {
        "request":  request,
        "sessions": sessions,
    })


# ─────────────────────────────────────────
# New chat
# ─────────────────────────────────────────
@app.post("/new_chat")
async def new_chat():
    new_session_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    await session_service.create_session(
        app_name=APP_NAME,
        user_id=USER_ID,
        session_id=new_session_id,
    )
    session_meta[new_session_id] = {
        "name":       "New Chat",
        "created_at": now,
    }
    return JSONResponse({
        "session_id": new_session_id,
        "name":       "New Chat",
        "created_at": now,
    })


# ─────────────────────────────────────────
# Rename session
# ─────────────────────────────────────────
@app.patch("/session/{session_id}/rename")
async def rename_session(session_id: str, request: Request):
    body = await request.json()
    new_name = body.get("name", "").strip()
    if not new_name:
        return JSONResponse({"error": "Name cannot be empty"}, status_code=400)
    if session_id not in session_meta:
        session_meta[session_id] = {"created_at": datetime.now(timezone.utc).isoformat()}
    session_meta[session_id]["name"] = new_name
    return JSONResponse({"session_id": session_id, "name": new_name})


# ─────────────────────────────────────────
# Delete session
# ─────────────────────────────────────────
@app.delete("/session/{session_id}")
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


# ─────────────────────────────────────────
# Session chat history
# ─────────────────────────────────────────
@app.get("/session/{session_id}/history")
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
        else:
            if not getattr(event, "partial", False):
                messages.append({"role": "bot", "text": text})

    return JSONResponse({"messages": messages})


# ─────────────────────────────────────────
# Streaming chat
# ─────────────────────────────────────────
@app.get("/chat/stream")
async def chat_stream_endpoint(user_input: str, session_id: str):
    if session_id in session_meta and session_meta[session_id].get("name") == "New Chat":
        session_meta[session_id]["name"] = user_input[:30].strip() + ("…" if len(user_input) > 30 else "")

    async def event_generator():
        async for chunk in chat_stream(user_input, session_id):
            yield f"data: {json.dumps(chunk)}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


# ─────────────────────────────────────────
# Graceful shutdown
# ─────────────────────────────────────────
@app.on_event("shutdown")
async def shutdown_event():
    logger.info("Jarvis is shutting down gracefully...")

    try:
        if runner and hasattr(runner, "shutdown"):
            await asyncio.wait_for(runner.shutdown(), timeout=5.0)
    except (asyncio.TimeoutError, Exception) as e:
        logger.warning(f"Runner shutdown warning (safe to ignore): {e}")

    logger.info("Shutdown complete.")


# ─────────────────────────────────────────
# Run
# ─────────────────────────────────────────
# uvicorn app:app --host 0.0.0.0 --port 8000
# uvicorn app:app --host 0.0.0.0 --port 8000 --reload
