# agent.py
# Jarvis Email Agent — main ADK orchestrator.
# Wires together the LlmAgent with Gmail tools, session management,
# and a streaming chat interface.

from __future__ import annotations

import os
import certifi
import asyncio

from google.adk.agents import LlmAgent
from google.adk.apps import App
from google.adk.sessions import InMemorySessionService
from google.adk import Runner
import google.genai.types as types

from config import config
from logger import get_logger
from prompts import EMAIL_AGENT_INSTRUCTION
from tools import EMAIL_TOOLS
from subagent import kb_loader, email_workflow

logger = get_logger(__name__)

# ── Constants ─────────────────────────────────────────────────
APP_NAME = config.APP_NAME
USER_ID  = config.USER_ID
MODEL    = config.MODEL

# ── Environment ───────────────────────────────────────────────
os.environ["GOOGLE_API_KEY"]           = config.GOOGLE_API_KEY.strip()
os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "0"
os.environ["SSL_CERT_FILE"]            = certifi.where()
os.environ["REQUESTS_CA_BUNDLE"]       = certifi.where()


# ─────────────────────────────────────────────────────────────
# KNOWLEDGE BASE INJECTION
# Augment the system prompt with loaded KB context so the agent
# understands Lagozon's org structure, contacts, and templates.
# ─────────────────────────────────────────────────────────────

def _build_system_instruction() -> str:
    kb_context = kb_loader.get_combined_context()
    if kb_context:
        return (
            EMAIL_AGENT_INSTRUCTION
            + "\n\n═══════════════════════════════════════════════════════════════\n"
            + " KNOWLEDGE BASE (loaded at startup)\n"
            + "═══════════════════════════════════════════════════════════════\n"
            + kb_context
        )
    return EMAIL_AGENT_INSTRUCTION


# ─────────────────────────────────────────────────────────────
# ROOT AGENT
# The LlmAgent receives all 6 Gmail tools. The ADK framework
# automatically generates JSON schemas from each function's
# type hints and docstrings.
# ─────────────────────────────────────────────────────────────

root_agent = LlmAgent(
    name        = "jarvis_email_agent",
    model       = MODEL,
    instruction = _build_system_instruction(),
    tools       = EMAIL_TOOLS,
)

logger.info(
    f"[Agent] Root agent '{root_agent.name}' initialised | "
    f"model={MODEL} | tools={[t.__name__ for t in EMAIL_TOOLS]}"
)


# ─────────────────────────────────────────────────────────────
# ADK APP
# ─────────────────────────────────────────────────────────────

jarvis_app = App(
    name       = APP_NAME,
    root_agent = root_agent,
)


# ─────────────────────────────────────────────────────────────
# SESSION SERVICE  (in-memory; swap for DatabaseSessionService in prod)
# ─────────────────────────────────────────────────────────────

session_service = InMemorySessionService()


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
    """Return existing in-memory session or create a new one."""
    logger.info(f"[Session] get_or_create | user={user_id} session={session_id}")

    session = await session_service.get_session(
        app_name   = APP_NAME,
        user_id    = user_id,
        session_id = session_id,
    )

    if session is None:
        logger.info(f"[Session] Creating new session: {session_id}")
        session = await session_service.create_session(
            app_name   = APP_NAME,
            user_id    = user_id,
            session_id = session_id,
        )
    else:
        logger.info(f"[Session] Existing session found: {session_id}")

    return session


# ─────────────────────────────────────────────────────────────
# PRE-PROCESSING HOOK
# Run intent parsing & validation BEFORE sending to the ADK loop.
# This gives cleaner errors and enriches the user message with
# resolved contacts and auto-generated bodies when needed.
# ─────────────────────────────────────────────────────────────

def _preprocess_input(user_input: str) -> tuple[str, str | None]:
    """
    Parse and validate the user intent. If validation passes, return the
    original input unchanged (the agent handles tool calls). If validation
    fails, return an error string to surface to the user immediately.

    Returns:
        (user_input, error_message_or_None)
    """
    intent, errors = email_workflow.parse_and_validate(user_input)

    if intent.get("action") == "unknown":
        return user_input, None   # Let the agent handle ambiguous input

    if errors:
        error_str = (
            "⚠️ I noticed some issues with your request before contacting Gmail:\n"
            + "\n".join(f"  • {e}" for e in errors)
            + "\n\nPlease correct the above and try again."
        )
        return user_input, error_str

    logger.info(f"[Agent] Pre-processing OK | action={intent.get('action')}")
    return user_input, None


# ─────────────────────────────────────────────────────────────
# STREAMING CHAT FUNCTION
# ─────────────────────────────────────────────────────────────

async def chat_stream(user_input: str, session_id: str):
    """
    Stream the agent's final response for a given user message.

    The function runs a pre-processing step to catch obvious errors
    (invalid emails, missing thread IDs) before entering the ADK loop.

    Args:
        user_input:  Raw message from the end user.
        session_id:  Unique identifier for this conversation session.

    Yields:
        str — Text chunks of the agent's final response.
    """
    logger.info(
        f"[Chat] chat_stream | session={session_id} | input={user_input!r}"
    )

    await get_or_create_session(USER_ID, session_id)

    # ── Pre-processing validation ─────────────────────────────
    _, pre_error = _preprocess_input(user_input)
    if pre_error:
        yield pre_error
        return

    # ── ADK agent loop ────────────────────────────────────────
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
                    f"[Chat] Final chunk | session={session_id} | "
                    f"len={len(part.text)}"
                )
                yield part.text


# ─────────────────────────────────────────────────────────────
# SIMPLE (NON-STREAMING) CHAT  — convenience wrapper
# ─────────────────────────────────────────────────────────────

async def chat(user_input: str, session_id: str) -> str:
    """
    Non-streaming variant of chat_stream.

    Collects all chunks and returns the complete response as a string.
    Use chat_stream for real-time UIs; use this for scripts and tests.
    """
    chunks: list[str] = []
    async for chunk in chat_stream(user_input, session_id):
        chunks.append(chunk)
    return "".join(chunks)
