# quotation_agent/run.py
"""
Standalone CLI entry-point for the Quotation Agent.
"""

import sys
import asyncio
from pathlib import Path

# ── Ensure both the agent dir and root dir are on sys.path ──────────────────
_AGENT_DIR = Path(__file__).resolve().parent   # quotation_agent/
_ROOT_DIR  = _AGENT_DIR.parent                 # project root
for _p in (_AGENT_DIR, _ROOT_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
# ─────────────────────────────────────────────────────────────────────────────

from google.genai import types
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService

from quotations_agent.agent import quotation_agent
from quotations_agent.prompts import HELP_TEXT

APP_NAME = "quotation_chatbot_app"
USER_ID  = "user_001"

_DELETE_KEYWORDS = (
    "delete", "remove", "erase", "drop", "wipe", "purge", "clear",
)

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
        print(f"\nBot: {reply}")
        # ── Record both turns in history ──────────────────────
        _conversation_history.append({"role": "user",      "text": user_text})
        _conversation_history.append({"role": "assistant", "text": reply})


async def main() -> None:
    session_service = InMemorySessionService()
    runner = Runner(
        agent=quotation_agent,
        app_name=APP_NAME,
        session_service=session_service,
    )
    session = await session_service.create_session(
        app_name=APP_NAME,
        user_id=USER_ID,
    )

    print("=" * 52)
    print("           Quotation Chatbot")
    print("=" * 52)
    print(HELP_TEXT)

    while True:
        try:
            user_input = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n\nExiting chatbot. Goodbye!")
            break

        if not user_input:
            continue

        lower = user_input.lower()

        # ── Local commands ────────────────────────────────────
        if lower in ("/exit", "exit", "quit"):
            print("\nExiting chatbot. Goodbye!")
            break

        if lower in ("/help_quotation", "/help"):
            print(HELP_TEXT)
            continue

        if lower == "/history":
            if not _conversation_history:
                print("  (no history yet)")
            else:
                print("\n── Conversation History ──")
                for i, entry in enumerate(_conversation_history, 1):
                    role = "You" if entry["role"] == "user" else "Bot"
                    print(f"  [{i}] {role}: {entry['text'][:120]}")
            continue

        if lower == "/clear_history":
            _conversation_history.clear()
            print("  Conversation history cleared.\n")
            continue

        # ── Delete confirmation guard ─────────────────────────
        if any(kw in lower for kw in _DELETE_KEYWORDS):
            confirm = input(
                "\n  ⚠️  This looks like a delete operation.\n"
                "  Deletions are permanent and cannot be undone.\n"
                "  Confirm? (yes/no): "
            ).strip().lower()
            if confirm not in ("yes", "y"):
                print("  Deletion cancelled.\n")
                continue

        await send_message(runner, session.id, user_input)


if __name__ == "__main__":
    asyncio.run(main())
