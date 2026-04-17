# run.py
# ─────────────────────────────────────────────────────────────
# Simple CLI to run your Jarvis chatbot
# ─────────────────────────────────────────────────────────────

import asyncio
import sys
from chatbot import chat_stream

async def main():
    session_id = "cli_interactive_session"   # persistent session (history saved in DB)

    print("🤖 Jarvis is online! Type 'exit' or Ctrl+C to quit.")
    print("=" * 60)

    while True:
        try:
            user_input = input("\nYou: ").strip()

            if user_input.lower() in {"exit", "quit", "bye"}:
                print("\n👋 Goodbye!")
                break

            if not user_input:
                continue

            print("Jarvis: ", end="", flush=True)

            # Stream the final response
            async for chunk in chat_stream(user_input, session_id):
                print(chunk, end="", flush=True)

            print()  # new line after response

        except KeyboardInterrupt:
            print("\n\n👋 Goodbye!")
            break
        except Exception as e:
            print(f"\n❌ Error: {e}")
            break


if __name__ == "__main__":
    asyncio.run(main())
