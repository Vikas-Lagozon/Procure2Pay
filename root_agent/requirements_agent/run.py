# requirements_agent/run.py

import sys
import asyncio
from pathlib import Path

# ── Ensure both the agent dir and root dir are on sys.path ──────────────────
_AGENT_DIR = Path(__file__).resolve().parent
_ROOT_DIR  = _AGENT_DIR.parent
for _p in (_AGENT_DIR, _ROOT_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
# ─────────────────────────────────────────────────────────────────────────────

from google.genai import types
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService

from requirements_agent.agent import requirements_agent
from requirements_agent.prompts import HELP_TEXT

APP_NAME = "requirements_chatbot_app"
USER_ID  = "user_001"

_DELETE_KEYWORDS = (
    "delete", "remove", "erase", "drop", "wipe", "purge", "clear",
)


async def send_message(runner: Runner, session_id: str, text: str) -> None:
    async for event in runner.run_async(
        user_id=USER_ID,
        session_id=session_id,
        new_message=types.Content(
            role="user",
            parts=[types.Part(text=text)],
        ),
    ):
        if event.is_final_response() and event.content:
            for part in event.content.parts:
                if getattr(part, "text", None):
                    print(f"\nBot: {part.text}")


async def main() -> None:
    session_service = InMemorySessionService()
    runner = Runner(
        agent=requirements_agent,
        app_name=APP_NAME,
        session_service=session_service,
    )
    session = await session_service.create_session(
        app_name=APP_NAME,
        user_id=USER_ID,
    )

    print("=" * 52)
    print("        Requirements Chatbot")
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

        # ── Local commands handled before sending to agent ────
        if lower in ("/exit", "exit", "quit"):
            print("\nExiting chatbot. Goodbye!")
            break

        if lower == "/help":
            print(HELP_TEXT)
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

