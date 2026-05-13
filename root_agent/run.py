# run.py
# ─────────────────────────────────────────────────────────────
# Simple CLI to run Jarvis
# ─────────────────────────────────────────────────────────────

import sys
import uuid
import asyncio
from pathlib import Path

# ── Ensure root_agent/ is always on sys.path ────────────────────────────────
_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
# ─────────────────────────────────────────────────────────────────────────────

from chatbot import chat_stream, close as close_session

_DELETE_KEYWORDS = (
    "delete", "remove", "erase", "drop", "wipe", "purge", "clear",
)


async def main():
    # ── Fresh session per run ──────────────────────────────────
    # Using a UUID prevents stale PostgreSQL connections from previous runs, which can cause confusing errors.
    session_id = f"cli_{uuid.uuid4().hex}"

    print("🤖 Jarvis is online! Type 'exit' or Ctrl+C to quit.")
    print("=" * 60)

    while True:
        try:
            user_input = input("\nYou: ").strip()

            if not user_input:
                continue

            if user_input.lower() in {"exit", "quit", "bye"}:
                print("\n👋 Goodbye!")
                await close_session()
                break

            # ── Delete confirmation guard ──────────────────────
            if any(kw in user_input.lower() for kw in _DELETE_KEYWORDS):
                confirm = input(
                    "\n  ⚠️  This looks like a delete operation.\n"
                    "  Deletions are permanent and cannot be undone.\n"
                    "  Confirm? (yes/no): "
                ).strip().lower()
                if confirm not in ("yes", "y"):
                    print("  Deletion cancelled.\n")
                    continue

            print("Jarvis: ", end="", flush=True)

            async for chunk in chat_stream(user_input, session_id):
                print(chunk, end="", flush=True)

            print()  # newline after response

        except KeyboardInterrupt:
            print("\n\n👋 Goodbye!")
            await close_session()
            break
        except Exception as e:
            print(f"\nError: {e}")
            await close_session()
            break


if __name__ == "__main__":
    # ── Windows: explicitly use SelectorEventLoop to suppress the harmless
    if sys.platform == "win32":
        asyncio.run(main(), loop_factory=asyncio.SelectorEventLoop)
    else:
        asyncio.run(main())
