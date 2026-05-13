# root_agent/chatbot.py
# ─────────────────────────────────────────────────────────────
# Jarvis — Procure-to-Pay Root Agent
# ─────────────────────────────────────────────────────────────

import os
import sys
import certifi
from pathlib import Path

# ── Ensure root_agent/ is always on sys.path ────────────────────────────────
_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
# ─────────────────────────────────────────────────────────────────────────────

from config import config
from logger import get_logger
from prompt import SYSTEM_INSTRUCTION
from tools import ROOT_TOOLS  # ← direct-DB tools for Jarvis

from google.adk.agents import LlmAgent
from google.adk.apps import App
from google.adk.sessions import DatabaseSessionService
from google.adk import Runner
import google.genai.types as types

# ── Sub-agents ───────────────────────────────────────────────────────────────
# Import order matters: root modules first, then sub-agents to avoid
# sys.path pollution between packages.
from requirements_agent.agent import requirements_agent
from vendors_agent.agent import vendors_agent
from quotations_agent.agent import quotation_agent
from email_agent.agent import email_agent
# ─────────────────────────────────────────────────────────────────────────────

logger = get_logger(__name__)

# ── Constants ─────────────────────────────────────────────────
APP_NAME = "Jarvis"
USER_ID  = "user_001"
MODEL    = config.MODEL

# ── Environment Setup ─────────────────────────────────────────
if config.GOOGLE_API_KEY:
    os.environ["GOOGLE_API_KEY"] = config.GOOGLE_API_KEY.strip()

os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "0"
os.environ["SSL_CERT_FILE"]             = certifi.where()
os.environ["REQUESTS_CA_BUNDLE"]        = certifi.where()

PATH_TO_PYTHON = sys.executable


# ─────────────────────────────────────────────────────────────
# ROOT / ORCHESTRATOR AGENT
# ─────────────────────────────────────────────────────────────

root_agent = LlmAgent(
    name        = "jarvis_root_agent",
    model       = MODEL,
    instruction = SYSTEM_INSTRUCTION,
    tools       = ROOT_TOOLS,          # ← direct-DB tools
    sub_agents  = [
        requirements_agent,            # CRUD for requirement docs
        vendors_agent,                 # CRUD for vendor docs
        quotation_agent,               # CRUD for quotation docs  ← added
        email_agent,                   # all Gmail operations
    ],
)


# ─────────────────────────────────────────────────────────────
# APP
# ─────────────────────────────────────────────────────────────

jarvis_app = App(
    name       = APP_NAME,
    root_agent = root_agent,
)


# ─────────────────────────────────────────────────────────────
# DATABASE SESSION SERVICE
# ─────────────────────────────────────────────────────────────

session_service = DatabaseSessionService(
    db_url       = config.SQLALCHEMY_DATABASE_URI,
    connect_args = {
        "server_settings": {
            "search_path": config.DB_SCHEMA
        }
    },
)


# ─────────────────────────────────────────────────────────────
# RUNNER
# ─────────────────────────────────────────────────────────────

runner = Runner(
    app_name        = APP_NAME,
    agent           = root_agent,
    session_service = session_service,
)


# ─────────────────────────────────────────────────────────────
# SESSION HELPER
# ─────────────────────────────────────────────────────────────

async def get_or_create_session(user_id: str, session_id: str):
    logger.info(f"Getting or creating session: user_id={user_id}, session_id={session_id}")

    session = await session_service.get_session(
        app_name   = APP_NAME,
        user_id    = user_id,
        session_id = session_id,
    )

    if session is None:
        logger.info(f"Session not found. Creating new session: session_id={session_id}")
        session = await session_service.create_session(
            app_name   = APP_NAME,
            user_id    = user_id,
            session_id = session_id,
        )
    else:
        logger.info(f"Existing session found: session_id={session_id}")

    return session


# ─────────────────────────────────────────────────────────────
# GRACEFUL SHUTDOWN
# ─────────────────────────────────────────────────────────────

async def close():
    """
    Dispose the SQLAlchemy engine used by DatabaseSessionService.
    Call this once before the event loop exits to prevent lingering
    connection warnings on Windows (WinError 10053).
    """
    try:
        engine = getattr(session_service, "_engine", None)
        if engine is not None:
            await engine.dispose()
            logger.info("Session service engine disposed cleanly.")
    except Exception as exc:
        logger.debug(f"Engine dispose skipped: {exc}")


# ─────────────────────────────────────────────────────────────
# STREAMING CHAT FUNCTION
# ─────────────────────────────────────────────────────────────

async def chat_stream(user_input: str, session_id: str):
    """
    Stream the agent's final response for a given user message.

    Parameters
    ----------
    user_input : str
        Raw message from the end user.
    session_id : str
        Unique identifier for this conversation session.

    Yields
    ------
    str
        Text chunks of the agent's final response.
    """
    logger.info(f"chat_stream called | session_id={session_id} | input={user_input!r}")

    await get_or_create_session(USER_ID, session_id)

    content = types.Content(
        role  = "user",
        parts = [types.Part(text=user_input)],
    )

    events = runner.run_async(
        user_id     = USER_ID,
        session_id  = session_id,
        new_message = content,
    )

    async for event in events:

        if not getattr(event, "content", None) or not event.content.parts:
            continue

        if not event.is_final_response():
            continue

        for part in event.content.parts:
            if getattr(part, "text", None):
                logger.info(
                    f"Final response chunk | session_id={session_id} | "
                    f"length={len(part.text)}"
                )
                yield part.text