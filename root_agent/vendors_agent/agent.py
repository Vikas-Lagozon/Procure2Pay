# vendors_agent/agent.py
"""
VendorsChatbot: a Google ADK BaseAgent that routes incoming user messages
to the correct business-logic handler in tools.py.

Intent understanding lives here (no separate intent.py / classifier.py).
All heavy work is delegated to:
  - tools.py   →  CRUD + upload + Q&A handlers
  - utils.py   →  file detection, command parsing, formatting
  - prompts.py →  all text constants and prompt builders

Routing priority
────────────────
1. Bare file path typed directly        →  upload immediately
2. Slash command (/upload_vendor, etc.) →  direct handler, no LLM overhead
3. Everything else (natural language)   →  _detect_intent() → handler
"""

from __future__ import annotations

import sys
import re
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

from google.adk.agents import BaseAgent, LlmAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.apps import App
from google.adk.events import Event

from vendors_agent.config import config
from vendors_agent.logger import get_logger
from vendors_agent.prompts import HELP_TEXT, intent_prompt
from vendors_agent.tools import (
    _get_session_docs,
    handle_delete,
    handle_get,
    handle_list,
    handle_question,
    handle_update,
    handle_upload,
    make_text_event,
)
from vendors_agent.utils import extract_file_path, is_file_path, parse_command

logger = get_logger(__name__)
MODEL  = config.MODEL


# ─────────────────────────────────────────────────────────────
# INTENT DETECTION  (lives here — no separate intent.py)
# ─────────────────────────────────────────────────────────────

async def _detect_intent(
    ctx: InvocationContext,
    user_input: str,
    documents: dict,
) -> dict:
    """
    Call a short-lived LlmAgent to classify user_input and extract parameters.
    Always returns a valid dict — never raises.

    Example return values:
      {"intent": "delete", "params": {"record_ids": "all"}}
      {"intent": "upload", "params": {"file_path": "docs/vendor.docx"}}
      {"intent": "query",  "params": {"question": "..."}}
    """
    agent = LlmAgent(
        name="intent_detector",
        model=MODEL,
        instruction=intent_prompt(user_input, documents),
    )

    raw = ""
    async for event in agent.run_async(ctx):
        if event.is_final_response() and event.content:
            for part in event.content.parts:
                if getattr(part, "text", None):
                    raw += part.text

    return _parse_intent_response(raw, user_input)


def _parse_intent_response(raw: str, original_input: str) -> dict:
    """Parse LLM JSON output with a safe fallback to query intent."""
    text = re.sub(r"```(?:json)?", "", raw).strip(" `\n")

    for attempt in (text, text[text.find("{"):text.rfind("}") + 1]):
        try:
            result = json.loads(attempt)
            if "intent" in result:
                logger.info(
                    f"[agent] Intent: {result['intent']} | "
                    f"params: {result.get('params', {})}"
                )
                return result
        except Exception:
            continue

    logger.warning(f"[agent] Intent parse failed — defaulting to query. Raw: {raw[:200]}")
    return {"intent": "query", "params": {"question": original_input}}


# ─────────────────────────────────────────────────────────────
# VENDORS CHATBOT AGENT
# ─────────────────────────────────────────────────────────────

class VendorsChatbot(BaseAgent):
    """
    Natural-language → Vendor Management System.

    Accepts both slash commands and conversational English to manage
    vendor documents stored in MongoDB and on the local filesystem.

    Slash command shortcuts (all suffixed with _vendor)
    ────────────────────────────────────────────────────
    /upload_vendor <file_path>
    /list_vendor
    /get_vendor <record_id>
    /delete_vendor <record_id>
    /update_vendor <record_id> <new_file_path>
    /help_vendor

    Natural language is the primary interface for all operations.
    """

    def __init__(self) -> None:
        super().__init__(
            name="vendors_chatbot",
            description=(
                "Natural language vendor management — upload, list, get, "
                "delete, update vendor documents and ask questions about them."
            ),
        )

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:

        logger.info("[agent] VendorsChatbot invoked")

        # ── Collect user input ──────────────────────────────
        user_input = ""
        if ctx.user_content and ctx.user_content.parts:
            for part in ctx.user_content.parts:
                if getattr(part, "text", None):
                    user_input += part.text.strip()

        if not user_input:
            yield make_text_event(self.name, "Please enter a command or a question.")
            return

        # ── Priority 1: bare file path → upload immediately ─
        if is_file_path(user_input):
            async for ev in handle_upload(ctx, user_input, self.name):
                yield ev
            return

        # ── Priority 2: slash command shortcuts ─────────────
        cmd, args = parse_command(user_input)

        if cmd is not None:
            async for ev in self._route_slash_command(ctx, cmd, args, user_input):
                yield ev
            return

        # ── Priority 3: natural language → intent detection ─
        documents = _get_session_docs(ctx)
        intent    = await _detect_intent(ctx, user_input, documents)

        action = intent.get("intent", "query")
        params = intent.get("params", {})

        async for ev in self._route_intent(ctx, action, params, user_input):
            yield ev

    # ─────────────────────────────────────────────────────────
    # SLASH COMMAND ROUTER
    # ─────────────────────────────────────────────────────────

    async def _route_slash_command(
        self,
        ctx: InvocationContext,
        cmd: str,
        args: list[str],
        user_input: str,
    ) -> AsyncGenerator[Event, None]:

        # ── /upload_vendor ──
        if cmd in ("/upload_vendor", "/upload"):
            file_path = " ".join(args).strip() if args else ""
            if not file_path:
                yield make_text_event(
                    self.name,
                    "Usage: /upload_vendor <file_path>\n"
                    "Supported formats: .docx, .pdf, .txt, .md",
                )
            else:
                async for ev in handle_upload(ctx, file_path, self.name):
                    yield ev

        # ── /list_vendor ──
        elif cmd in ("/list_vendor", "/list"):
            async for ev in handle_list(ctx, self.name):
                yield ev

        # ── /get_vendor <record_id> ──
        elif cmd in ("/get_vendor", "/get"):
            if not args:
                yield make_text_event(self.name, "Usage: /get_vendor <record_id>")
            else:
                async for ev in handle_get(ctx, [args[0].strip()], self.name):
                    yield ev

        # ── /delete_vendor <record_id> ──
        elif cmd in ("/delete_vendor", "/delete"):
            if not args:
                yield make_text_event(self.name, "Usage: /delete_vendor <record_id>")
            else:
                async for ev in handle_delete(ctx, [args[0].strip()], self.name):
                    yield ev

        # ── /update_vendor <record_id> <new_file_path> ──
        elif cmd in ("/update_vendor", "/update"):
            if len(args) < 2:
                yield make_text_event(
                    self.name,
                    "Usage: /update_vendor <record_id> <new_file_path>",
                )
            else:
                async for ev in handle_update(
                    ctx, args[0].strip(), args[1].strip(), self.name
                ):
                    yield ev

        # ── /help_vendor ──
        elif cmd in ("/help_vendor", "/help"):
            yield make_text_event(self.name, HELP_TEXT)

        # ── Unknown slash command ──
        else:
            yield make_text_event(
                self.name,
                f"Unknown command: {cmd}\n"
                "Type /help_vendor to see all available commands.",
            )

    # ─────────────────────────────────────────────────────────
    # INTENT ROUTER (natural language path)
    # ─────────────────────────────────────────────────────────

    async def _route_intent(
        self,
        ctx: InvocationContext,
        action: str,
        params: dict,
        user_input: str,
    ) -> AsyncGenerator[Event, None]:

        if action == "upload":
            file_path = params.get("file_path", "").strip()
            if not file_path:
                file_path = extract_file_path(user_input) or ""
            if not file_path:
                yield make_text_event(
                    self.name,
                    "I understood you want to upload a vendor document.\n"
                    "Please provide the file path, for example:\n"
                    '  "Upload docs/dell_india_vendor.docx"',
                )
            else:
                async for ev in handle_upload(ctx, file_path, self.name):
                    yield ev

        elif action == "list":
            async for ev in handle_list(ctx, self.name):
                yield ev

        elif action == "get":
            record_ids = params.get("record_ids", [])
            async for ev in handle_get(ctx, record_ids, self.name):
                yield ev

        elif action == "delete":
            record_ids = params.get("record_ids", [])
            if not record_ids:
                yield make_text_event(
                    self.name,
                    "I understood you want to delete vendor record(s), but I couldn't "
                    "identify which one(s).\n\n"
                    "Try:\n"
                    '  "Delete the Dell vendor"\n'
                    '  "Delete all vendors"\n'
                    '  "Delete record <id>"',
                )
            else:
                async for ev in handle_delete(ctx, record_ids, self.name):
                    yield ev

        elif action == "update":
            record_id     = params.get("record_id", "").strip()
            new_file_path = params.get("new_file_path", "").strip()
            if not record_id or not new_file_path:
                yield make_text_event(
                    self.name,
                    "I understood you want to update a vendor, but I need "
                    "both the record identifier and the new file path.\n\n"
                    "Try:\n"
                    '  "Update the Dell vendor with docs/dell_v2.docx"\n'
                    '  "Replace record 6642abc123 with docs/vendor_v2.docx"',
                )
            else:
                async for ev in handle_update(
                    ctx, record_id, new_file_path, self.name
                ):
                    yield ev

        else:  # query (default)
            question = params.get("question", user_input)
            async for ev in handle_question(ctx, question, self.name):
                yield ev


# ─────────────────────────────────────────────────────────────
# EXPORTS
# ─────────────────────────────────────────────────────────────

vendors_agent = VendorsChatbot()

app = App(
    name="vendors_chatbot_app",
    root_agent=vendors_agent,
)

__all__ = ["vendors_agent", "app"]
