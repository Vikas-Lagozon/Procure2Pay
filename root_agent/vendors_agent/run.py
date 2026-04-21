# run.py  —  Vendors Agent

import asyncio
from google.genai import types
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService

from agent import chatbot_agent

APP_NAME = "vendors_chatbot_app"
USER_ID  = "user_001"

HELP_TEXT = """
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 Vendors Chatbot — Commands
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

UPLOAD  (file is stored in VENDORS/ with a timestamp prefix)
  <file_path>                      Type or paste the file path directly
  /upload <file_path>              Alternatively, use the /upload prefix
  Supported formats: .docx, .pdf, .txt, .md
  Example: /upload docs/dell_india_vendor.docx

READ
  /list                            List all vendor records in the database
  /get <record_id>                 Show full details of one vendor record

DELETE  (removes DB record + stored file — asks for confirmation)
  /delete <record_id>

UPDATE  (replaces stored file, re-extracts text, updates DB record)
  /update <record_id> <new_file_path>
  Example: /update 6642abc123 docs/dell_india_v2.docx

NATURAL LANGUAGE Q&A  (works even without uploading in this session)
  Ask anything in plain English — vendor documents are loaded from the
  database automatically. Examples:
    List all vendors that supply electronics
    Which vendors are ISO certified?
    What are the payment terms for Dell India?
    Which vendor offers the lowest price for laptops?
    Compare delivery lead times across all vendors
    How many vendors supply home appliances?
    Which vendors are marked as preferred?

MISC
  /help                            Show this message
  /exit                            Quit
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""


async def send_message(runner: Runner, session_id: str, text: str):
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


async def main():
    session_service = InMemorySessionService()
    runner = Runner(
        agent=chatbot_agent,
        app_name=APP_NAME,
        session_service=session_service,
    )
    session = await session_service.create_session(
        app_name=APP_NAME,
        user_id=USER_ID,
    )

    print("=" * 50)
    print("         Vendors Chatbot")
    print("=" * 50)
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

        if lower in ("/exit", "exit", "quit"):
            print("\nExiting chatbot. Goodbye!")
            break

        elif lower == "/help":
            print(HELP_TEXT)

        elif lower.startswith("/upload"):
            parts = user_input.split(maxsplit=1)
            if len(parts) < 2 or not parts[1].strip():
                print("\n  Usage: /upload <file_path>")
                print("  Supported: .docx, .pdf, .txt, .md\n")
                continue
            await send_message(runner, session.id, parts[1].strip())

        elif lower == "/list":
            await send_message(runner, session.id, "/list")

        elif lower.startswith("/get"):
            parts = user_input.split(maxsplit=1)
            if len(parts) < 2:
                print("\n  Usage: /get <record_id>\n")
                continue
            await send_message(runner, session.id, user_input)

        elif lower.startswith("/delete"):
            parts = user_input.split(maxsplit=1)
            if len(parts) < 2:
                print("\n  Usage: /delete <record_id>\n")
                continue
            record_id = parts[1].strip()
            confirm = input(
                f"\n  ⚠️  Permanently delete vendor record '{record_id}' and its stored file?\n"
                f"  This action cannot be undone. Confirm? (yes/no): "
            ).strip().lower()
            if confirm in ("yes", "y"):
                await send_message(runner, session.id, user_input)
            else:
                print("  Deletion cancelled.")

        elif lower.startswith("/update"):
            parts = user_input.split(maxsplit=2)
            if len(parts) < 3:
                print(
                    "\n  Usage: /update <record_id> <new_file_path>"
                    "\n  Example: /update 6642abc123 docs/dell_india_v2.docx\n"
                )
                continue
            await send_message(runner, session.id, user_input)

        else:
            await send_message(runner, session.id, user_input)


if __name__ == "__main__":
    asyncio.run(main())
