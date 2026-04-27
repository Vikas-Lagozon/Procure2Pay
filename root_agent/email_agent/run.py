#!/usr/bin/env python3
# run.py
# Entry point for the Jarvis Email Agent.
# Runs an interactive CLI loop with full session persistence.
#
# Usage:
#   python run.py                    # interactive mode
#   python run.py --once "your cmd" # single-shot mode (scripting)
#   python run.py --session my_id   # resume a named session

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from datetime import datetime

from logger import get_logger
from agent import chat_stream, chat, get_or_create_session, USER_ID

logger = get_logger(__name__)

# ── ANSI colour codes (disabled on non-TTY) ───────────────────
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
║          J A R V I S  —  Email Automation Agent             ║
║          Lagozon Technology Pvt. Ltd.                        ║
╚══════════════════════════════════════════════════════════════╝
{RESET}
{YELLOW}Type your email instruction in plain English.{RESET}
{YELLOW}Examples:{RESET}
  • Send a proposal to john@acme.com
  • Show my unread emails
  • Reply to thread <thread_id> saying delivery confirmed by Friday
  • Download all invoice attachments from last week
  • Attach /path/to/report.pdf to an email for alice@corp.com

{YELLOW}Commands:{RESET}
  {BOLD}exit{RESET} / {BOLD}quit{RESET}   — End the session
  {BOLD}session{RESET}        — Show current session ID
  {BOLD}clear{RESET}          — Clear terminal (history preserved)
  {BOLD}help{RESET}           — Show this help
"""


# ─────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────

def _print_banner() -> None:
    print(BANNER)


def _print_user(text: str) -> None:
    print(f"\n{BOLD}{GREEN}You:{RESET} {text}")


def _print_jarvis_start() -> None:
    print(f"\n{BOLD}{CYAN}Jarvis:{RESET} ", end="", flush=True)


def _print_chunk(chunk: str) -> None:
    print(chunk, end="", flush=True)


def _print_newline() -> None:
    print()


def _print_error(msg: str) -> None:
    print(f"\n{RED}[Error] {msg}{RESET}")


def _print_info(msg: str) -> None:
    print(f"{YELLOW}{msg}{RESET}")


def _show_help() -> None:
    _print_banner()


async def _stream_response(user_input: str, session_id: str) -> None:
    """Stream Jarvis's response to stdout."""
    _print_jarvis_start()
    has_output = False
    try:
        async for chunk in chat_stream(user_input, session_id):
            _print_chunk(chunk)
            has_output = True
    except Exception as exc:
        _print_error(f"Agent error: {exc}")
        logger.error(f"[run] Agent error: {exc}", exc_info=True)
    finally:
        if not has_output:
            print(f"{YELLOW}(No response from agent.){RESET}", end="")
        _print_newline()


# ─────────────────────────────────────────────────────────────
# INTERACTIVE LOOP
# ─────────────────────────────────────────────────────────────

async def interactive_loop(session_id: str) -> None:
    """
    Run an interactive REPL for the Jarvis email agent.

    Maintains conversation history within the ADK InMemorySessionService
    for the duration of the process. Each session_id is a separate
    conversation context.
    """
    _print_banner()
    _print_info(f"Session ID: {session_id}")
    _print_info(f"Started   : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")

    await get_or_create_session(USER_ID, session_id)

    while True:
        try:
            user_input = input(f"{BOLD}You:{RESET} ").strip()
        except (EOFError, KeyboardInterrupt):
            _print_info("\n\nSession ended. Goodbye!")
            break

        if not user_input:
            continue

        command = user_input.lower()

        if command in ("exit", "quit", "bye"):
            _print_info("\nGoodbye! Session closed.")
            break

        if command == "session":
            _print_info(f"Current session ID: {session_id}")
            continue

        if command == "clear":
            print("\033[2J\033[H" if _IS_TTY else "")
            _print_banner()
            continue

        if command == "help":
            _show_help()
            continue

        _print_user(user_input)
        await _stream_response(user_input, session_id)


# ─────────────────────────────────────────────────────────────
# SINGLE-SHOT MODE
# ─────────────────────────────────────────────────────────────

async def single_shot(command: str, session_id: str) -> None:
    """
    Execute a single command and print the full response.
    Useful for scripting and CI pipelines.
    """
    logger.info(f"[run] Single-shot | command={command!r} | session={session_id}")
    await get_or_create_session(USER_ID, session_id)
    response = await chat(command, session_id)
    print(response)


# ─────────────────────────────────────────────────────────────
# ARGUMENT PARSER
# ─────────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog        = "jarvis",
        description = "Jarvis — Natural Language Email Agent (Lagozon Technology)",
    )
    parser.add_argument(
        "--session",
        metavar = "SESSION_ID",
        default = "",
        help    = "Resume or start a named session (default: auto-generated UUID).",
    )
    parser.add_argument(
        "--once",
        metavar = "COMMAND",
        default = "",
        help    = "Execute a single command and exit (non-interactive mode).",
    )
    return parser.parse_args()


# ─────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────

async def main() -> None:
    args       = _parse_args()
    session_id = args.session or str(uuid.uuid4())

    logger.info(f"[run] Starting Jarvis | session={session_id}")

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
