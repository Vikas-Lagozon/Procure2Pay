# requirements_agent/agent.py
"""
RequirementsChatbot — fully natural-language-driven CRUD + Q&A agent.

Intent detection is handled here via an LlmAgent call — no separate
intent.py file is needed.
"""

import sys
import re
import json
from typing import AsyncGenerator
from pathlib import Path

# ── Ensure both the agent dir and root dir are on sys.path ──────────────────
_AGENT_DIR = Path(__file__).resolve().parent
_ROOT_DIR  = _AGENT_DIR.parent
for _p in (_AGENT_DIR, _ROOT_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
# ─────────────────────────────────────────────────────────────────────────────

from google.adk.agents import BaseAgent, LlmAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event
from google.adk.apps import App

from requirements_agent.config import config
from requirements_agent.utils import is_file_path, extract_file_path
from requirements_agent.prompts import intent_prompt
from requirements_agent.tools import (
    text_event,
    handle_upload,
    handle_list,
    handle_get,
    handle_delete,
    handle_update,
    handle_question,
)
from requirements_agent.logger import get_logger

logger = get_logger(__name__)

MODEL = config.MODEL


# ─────────────────────────────────────────────────────────────
# INTENT DETECTION
# ─────────────────────────────────────────────────────────────

async def _detect_intent(
    ctx: InvocationContext,
    user_input: str,
) -> dict:
    """
    Call a small LlmAgent to classify user_input and extract parameters.
    Always returns a valid dict — never raises.

    Documents context is NOT loaded from session state — the agent is
    stateless.  Intent detection works on the raw user message alone.

    Returns e.g.:
      {"intent": "delete", "params": {"record_ids": "all"}}
      {"intent": "upload", "params": {"file_path": "docs/req.docx"}}
      {"intent": "query",  "params": {"question": "..."}}
    """
    agent = LlmAgent(
        name="intent_detector",
        model=MODEL,
        # Pass an empty documents dict — no session state dependency
        instruction=intent_prompt(user_input, {}),
    )

    raw = ""
    async for event in agent.run_async(ctx):
        if event.is_final_response() and event.content:
            for part in event.content.parts:
                if getattr(part, "text", None):
                    raw += part.text

    return _parse_intent_response(raw, user_input)


def _parse_intent_response(raw: str, original_input: str) -> dict:
    """Parse LLM JSON output with safe fallback to query."""
    text = re.sub(r"```(?:json)?", "", raw).strip(" `\n")

    for attempt in (text, text[text.find("{"):text.rfind("}") + 1]):
        try:
            result = json.loads(attempt)
            if "intent" in result:
                logger.info(
                    f"Intent: {result['intent']} | params: {result.get('params', {})}"
                )
                return result
        except Exception:
            continue

    logger.warning(f"Intent parse failed — defaulting to query. Raw: {raw[:200]}")
    return {"intent": "query", "params": {"question": original_input}}


# ─────────────────────────────────────────────────────────────
# AGENT
# ─────────────────────────────────────────────────────────────

class RequirementsChatbot(BaseAgent):

    def __init__(self):
        super().__init__(
            name="requirements_chatbot",
            description=(
                "Natural language requirements management — upload, list, "
                "get, delete, update documents and ask questions about them."
            ),
        )

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:

        logger.info("Chatbot invoked")

        # ── Collect user text ─────────────────────────────────
        user_input = ""
        if ctx.user_content and ctx.user_content.parts:
            for part in ctx.user_content.parts:
                if getattr(part, "text", None):
                    user_input += part.text.strip()

        if not user_input:
            yield text_event(self.name, "Please enter a command or a question.")
            return

        # ── Bare file path typed directly → upload immediately ─
        if is_file_path(user_input):
            async for ev in handle_upload(self.name, ctx, user_input):
                yield ev
            return

        # ── Detect intent via LLM (stateless — no session docs) ──
        intent = await _detect_intent(ctx, user_input)

        action = intent.get("intent", "query")
        params = intent.get("params", {})

        # ── Route ─────────────────────────────────────────────

        if action == "upload":
            file_path = params.get("file_path", "").strip()
            if not file_path:
                file_path = extract_file_path(user_input) or ""
            if not file_path:
                yield text_event(
                    self.name,
                    "I understood you want to upload a requirement.\n"
                    "Please provide the file path, for example:\n"
                    '  "Upload docs/requirement_v1.docx"',
                )
            else:
                async for ev in handle_upload(self.name, ctx, file_path):
                    yield ev

        elif action == "list":
            async for ev in handle_list(self.name, ctx):
                yield ev

        elif action == "get":
            record_ids = params.get("record_ids", [])
            async for ev in handle_get(self.name, ctx, record_ids):
                yield ev

        elif action == "delete":
            record_ids = params.get("record_ids", [])
            if not record_ids:
                yield text_event(
                    self.name,
                    "I understood you want to delete requirement(s), but I couldn't "
                    "identify which one(s).\n\n"
                    "Try:\n"
                    '  "Delete the laptop requirement"\n'
                    '  "Delete all requirements"\n'
                    '  "Delete record <id>"',
                )
            else:
                async for ev in handle_delete(self.name, ctx, record_ids):
                    yield ev

        elif action == "update":
            record_id     = params.get("record_id", "").strip()
            new_file_path = params.get("new_file_path", "").strip()
            if not record_id or not new_file_path:
                yield text_event(
                    self.name,
                    "I understood you want to update a requirement, but I need "
                    "both the record identifier and the new file path.\n\n"
                    "Try:\n"
                    '  "Update the laptop requirement with docs/req_v2.docx"\n'
                    '  "Replace record 6642abc123 with docs/req_v2.docx"',
                )
            else:
                async for ev in handle_update(
                    self.name, ctx, record_id, new_file_path
                ):
                    yield ev

        else:  # query (default)
            question = params.get("question", user_input)
            async for ev in handle_question(self.name, ctx, question):
                yield ev


# ─────────────────────────────────────────────────────────────
# EXPORTS
# ─────────────────────────────────────────────────────────────

requirements_agent = RequirementsChatbot()

app = App(
    name="requirements_chatbot_app",
    root_agent=requirements_agent,
)

__all__ = ["requirements_agent", "app"]
