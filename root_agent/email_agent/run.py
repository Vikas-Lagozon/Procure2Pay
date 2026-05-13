#!/usr/bin/env python3
# run.py — Email Agent
"""
Works in TWO modes:
  1. Standalone : python run.py            (from email_agent/)
                  python email_agent/run.py (from root_agent/)
  2. As package : imported by root_agent/chatbot.py
"""

import argparse
import asyncio
import sys
import uuid
from pathlib import Path

# ── Ensure root_agent/ is on sys.path so `email_agent.*` resolves ──
_ROOT = Path(__file__).resolve().parent.parent   # → root_agent/
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from google.genai import types
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService

from email_agent.agent import email_agent
from email_agent.logger import get_logger

logger = get_logger(__name__)

APP_NAME = "email_app"
USER_ID  = "user_001"

# ── ANSI colour codes ───────────────────────────────────────
_IS_TTY = sys.stdout.isatty()

CYAN   = "\033[96m"  if _IS_TTY else ""
GREEN  = "\033[92m"  if _IS_TTY else ""
YELLOW = "\033[93m"  if _IS_TTY else ""
RED    = "\033[91m"  if _IS_TTY else ""
BOLD   = "\033[1m"   if _IS_TTY else ""
RESET  = "\033[0m"   if _IS_TTY else ""

BANNER = f"""
{BOLD}{CYAN}
╔══════════════════════════════════════════════════════════════╗
║          E M A I L  A G E N T  —  Email Automation Agent     ║
║          Lagozon Technology Pvt. Ltd.                        ║
╚══════════════════════════════════════════════════════════════╝
{RESET}
{YELLOW}Type your email instruction in plain English.{RESET}
"""

HELP_TEXT = """
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 Email Agent — Commands
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

NATURAL LANGUAGE (Recommended)
  Send a proposal to john@acme.com
  Show my unread emails
  Reply to thread <thread_id> saying "Approved"
  Download all attachments from last week

SPECIAL COMMANDS
  /help           — Show this help
  /session        — Show current session ID
  /history        — Show conversation history
  /clear_history  — Clear conversation history
  /clear          — Clear terminal screen
  exit / quit     — End session

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

# ── Conversation history ─────────────────────────────────────
# Each entry: {"role": "user"|"assistant", "text": str}
_conversation_history: list[dict] = []
_MAX_HISTORY_TURNS = 20          # keep last N turns (user+assistant pairs)


def _build_message_with_history(user_text: str) -> str:
    """
    Prepend a concise conversation history block to the raw user message so
    the stateless agent has enough context to resolve pronouns and references.
    """
    if not _conversation_history:
        return user_text

    lines = ["[CONVERSATION HISTORY]"]
    for entry in _conversation_history[-(_MAX_HISTORY_TURNS * 2):]:
        role  = "User"      if entry["role"] == "user"      else "Assistant"
        lines.append(f"{role}: {entry['text']}")
    lines.append("[END HISTORY]")
    lines.append("")
    lines.append(user_text)
    return "\n".join(lines)


async def send_message(runner: Runner, session_id: str, user_text: str) -> None:
    """Send one enriched message and collect the agent reply."""
    enriched = _build_message_with_history(user_text)

    reply_parts: list[str] = []

    async for event in runner.run_async(
        user_id=USER_ID,
        session_id=session_id,
        new_message=types.Content(
            role="user",
            parts=[types.Part(text=enriched)],
        ),
    ):
        if event.is_final_response() and event.content:
            for part in event.content.parts:
                if getattr(part, "text", None):
                    reply_parts.append(part.text)

    reply = "\n".join(reply_parts).strip()

    if reply:
        print(f"\n{BOLD}{CYAN}Email Agent:{RESET} {reply}")
        # ── Record both turns in history ──────────────────────
        _conversation_history.append({"role": "user",      "text": user_text})
        _conversation_history.append({"role": "assistant", "text": reply})


async def interactive_loop(session_id: str) -> None:
    session_service = InMemorySessionService()

    await session_service.create_session(
        app_name=APP_NAME,
        user_id=USER_ID,
        session_id=session_id,
    )

    runner = Runner(
        agent=email_agent,
        app_name=APP_NAME,
        session_service=session_service,
    )

    print("=" * 60)
    print("               EMAIL AGENT")
    print("=" * 60)
    print(BANNER)
    print(HELP_TEXT)
    print(f"Session ID: {session_id}\n")

    while True:
        try:
            user_input = input(f"{BOLD}You:{RESET} ").strip()
        except (EOFError, KeyboardInterrupt):
            print(f"\n\n{RED}Session ended. Goodbye!{RESET}")
            break

        if not user_input:
            continue

        lower = user_input.lower()

        if lower in ("exit", "quit", "bye"):
            print(f"\n{YELLOW}Goodbye!{RESET}")
            break

        elif lower == "/help":
            print(HELP_TEXT)
            continue

        elif lower == "/session":
            print(f"{YELLOW}Current Session ID: {session_id}{RESET}")
            continue

        elif lower == "/history":
            if not _conversation_history:
                print("  (no history yet)")
            else:
                print("\n── Conversation History ──")
                for i, entry in enumerate(_conversation_history, 1):
                    role = "You" if entry["role"] == "user" else "Agent"
                    print(f"  [{i}] {role}: {entry['text'][:120]}")
            continue

        elif lower == "/clear_history":
            _conversation_history.clear()
            print("  Conversation history cleared.\n")
            continue

        elif lower == "/clear":
            print("\033[2J\033[H" if _IS_TTY else "")
            print(BANNER)
            continue

        await send_message(runner, session_id, user_input)


async def single_shot(command: str, session_id: str) -> None:
    session_service = InMemorySessionService()

    await session_service.create_session(
        app_name=APP_NAME,
        user_id=USER_ID,
        session_id=session_id,
    )

    runner = Runner(
        agent=email_agent,
        app_name=APP_NAME,
        session_service=session_service,
    )
    await send_message(runner, session_id, command)


def _parse_args():
    parser = argparse.ArgumentParser(
        prog="email_agent",
        description="Email Agent — Natural Language Email Agent"
    )
    parser.add_argument("--session", default="", help="Session ID")
    parser.add_argument("--once", default="", help="Single command mode")
    return parser.parse_args()


async def main():
    args = _parse_args()
    session_id = args.session or str(uuid.uuid4())

    logger.info(f"Starting Email Agent | session={session_id}")

    if args.once:
        await single_shot(args.once, session_id)
    else:
        await interactive_loop(session_id)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(0)
