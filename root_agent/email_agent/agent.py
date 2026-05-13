# agent.py — Email Agent

import os
import sys
from pathlib import Path
import certifi

from google.adk.agents import LlmAgent
from google.adk.apps import App

# ─────────────────────────────────────────────────────────────
_AGENT_DIR = Path(__file__).resolve().parent
_ROOT_DIR  = _AGENT_DIR.parent
for _p in (_AGENT_DIR, _ROOT_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
# ─────────────────────────────────────────────────────────────

from email_agent.config import config
from email_agent.logger import get_logger
from email_agent.prompts import EMAIL_AGENT_INSTRUCTION
from email_agent.tools import EMAIL_TOOLS
from email_agent.utils import kb_loader

logger = get_logger(__name__)

# ── Constants ─────────────────────────────────────────────────
APP_NAME = config.APP_NAME
USER_ID  = config.USER_ID
MODEL    = config.MODEL

# ── Environment Setup ────────────────────────────────────────
os.environ["GOOGLE_API_KEY"] = config.GOOGLE_API_KEY.strip()
os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "0"
os.environ["SSL_CERT_FILE"] = certifi.where()
os.environ["REQUESTS_CA_BUNDLE"] = certifi.where()


# ─────────────────────────────────────────────────────────────
# KNOWLEDGE BASE BUILDER
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
# EMAIL AGENT
# ─────────────────────────────────────────────────────────────
email_agent = LlmAgent(
    name="email_agent",
    model=MODEL,
    description=(
        "Intelligent Email Automation Assistant with Gmail integration. "
        "Handles sending emails, reading threads, managing attachments, "
        "and more via natural language and commands."
    ),
    instruction=_build_system_instruction(),
    tools=EMAIL_TOOLS,
)

app = App(
    name="email_app",
    root_agent=email_agent,
)

__all__ = ["email_agent", "app"]
