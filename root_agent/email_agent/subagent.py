# subagent.py
# Reusable subagent logic — intent parsing, email validation,
# thread tracking, attachment management, and knowledge base loading.
# Designed to be imported by agent.py and any future agents.

from __future__ import annotations

import asyncio
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import google.generativeai as genai

from config import config
from logger import get_logger
from nosql_db import MongoCollection
from prompts import INTENT_EXTRACTION_PROMPT, EMAIL_BODY_GENERATION_PROMPT

logger = get_logger(__name__)

# ── Configure Gemini for direct subagent calls ────────────────
genai.configure(api_key=config.GOOGLE_API_KEY)
_gemini = genai.GenerativeModel(model_name=config.MODEL)

# ── MongoDB collections ───────────────────────────────────────
_threads_col  = MongoCollection("threads")
_contacts_col = MongoCollection("contacts")
_emails_col   = MongoCollection("emails")


# ═════════════════════════════════════════════════════════════
# INTENT PARSER
# ═════════════════════════════════════════════════════════════

class IntentParser:
    """
    Converts natural-language user instructions into structured JSON intents.

    Uses Gemini to extract action, recipients, subject, body, attachments,
    and thread references from free-form text.
    """

    # Expected action values
    VALID_ACTIONS = {
        "send_email",
        "reply_to_email",
        "read_emails",
        "get_thread",
        "download_attachments",
        "attach_files",
    }

    def parse(self, user_instruction: str) -> dict[str, Any]:
        """
        Parse a natural-language instruction into a structured intent dict.

        Args:
            user_instruction: Free-form user text, e.g.
                "Send the proposal to john@acme.com with the PDF attached."

        Returns:
            Structured intent dict. On parse failure, returns a dict with
            action="unknown" and an "error" key.
        """
        logger.info(f"[IntentParser] Parsing: {user_instruction!r}")

        prompt = INTENT_EXTRACTION_PROMPT.format(
            user_instruction=user_instruction
        )

        try:
            response = _gemini.generate_content(prompt)
            raw      = response.text.strip()

            # Strip markdown fences if present
            raw = re.sub(r"^```(?:json)?\s*", "", raw)
            raw = re.sub(r"\s*```$", "", raw)

            intent = json.loads(raw)
            intent = self._normalise(intent)

            logger.info(f"[IntentParser] Intent: action={intent.get('action')}")
            return intent

        except json.JSONDecodeError as exc:
            logger.error(f"[IntentParser] JSON parse failed: {exc} | raw={raw!r}")
            return {"action": "unknown", "error": f"Failed to parse intent: {exc}"}
        except Exception as exc:
            logger.error(f"[IntentParser] Unexpected error: {exc}")
            return {"action": "unknown", "error": str(exc)}

    def _normalise(self, intent: dict) -> dict:
        """Apply defaults and type coercions to a raw intent dict."""
        defaults = {
            "action":      "unknown",
            "to":          [],
            "cc":          [],
            "bcc":         [],
            "subject":     "",
            "body":        "",
            "attachments": [],
            "thread_id":   "",
            "message_id":  "",
            "query":       "is:unread",
            "max_results": config.EMAIL_FETCH_MAX_RESULTS,
            "download_dir": "",
        }
        for key, default in defaults.items():
            if key not in intent or intent[key] is None:
                intent[key] = default

        # Ensure list fields are lists
        for list_field in ("to", "cc", "bcc", "attachments"):
            if isinstance(intent[list_field], str):
                intent[list_field] = (
                    [intent[list_field]] if intent[list_field] else []
                )

        # Validate action
        if intent["action"] not in self.VALID_ACTIONS:
            logger.warning(
                f"[IntentParser] Unknown action '{intent['action']}' — setting to 'unknown'."
            )
            intent["action"] = "unknown"

        return intent


# ═════════════════════════════════════════════════════════════
# EMAIL VALIDATOR
# ═════════════════════════════════════════════════════════════

class EmailValidator:
    """
    Validates email intent fields before any API call is made.

    Checks email address syntax, required fields by action type,
    and file attachment validity.
    """

    _EMAIL_RE = re.compile(
        r"^[a-zA-Z0-9_.+\-]+@[a-zA-Z0-9\-]+\.[a-zA-Z0-9\-.]+$"
    )

    # Actions that require at least one 'to' recipient
    _REQUIRES_TO = {"send_email", "reply_to_email"}
    # Actions that require a thread_id
    _REQUIRES_THREAD = {"reply_to_email", "get_thread"}

    def validate(self, intent: dict) -> tuple[bool, list[str]]:
        """
        Validate an intent dict.

        Returns:
            (is_valid, errors) — True + [] on success, False + messages on failure.
        """
        errors: list[str] = []
        action = intent.get("action", "")

        # Recipient validation
        for field in ("to", "cc", "bcc"):
            for addr in intent.get(field, []):
                if not self._EMAIL_RE.match(addr.strip()):
                    errors.append(f"Invalid email in '{field}': '{addr}'")

        if action in self._REQUIRES_TO and not intent.get("to"):
            errors.append(f"Action '{action}' requires at least one 'to' recipient.")

        if action in self._REQUIRES_THREAD and not intent.get("thread_id"):
            errors.append(
                f"Action '{action}' requires 'thread_id'. "
                "Use read_emails or get_thread to find it first."
            )

        # Attachment existence check (light — tools.py does the deep check)
        for fp in intent.get("attachments", []):
            if fp and not Path(fp).exists():
                errors.append(f"Attachment file not found: '{fp}'")

        if errors:
            logger.warning(f"[EmailValidator] Validation errors: {errors}")
            return False, errors

        return True, []


# ═════════════════════════════════════════════════════════════
# EMAIL BODY GENERATOR
# ═════════════════════════════════════════════════════════════

class EmailBodyGenerator:
    """
    Generates professional email body text when the user's instruction
    does not include an explicit body.
    """

    def generate(
        self,
        subject: str,
        recipients: list[str],
        purpose: str,
    ) -> str:
        """
        Generate a professional Lagozon-branded email body.

        Args:
            subject:    Email subject line.
            recipients: List of recipient addresses.
            purpose:    Natural-language description of what the email should say.

        Returns:
            Generated email body string including signature.
        """
        logger.info(f"[EmailBodyGenerator] Generating body for subject='{subject}'")

        prompt = EMAIL_BODY_GENERATION_PROMPT.format(
            subject=subject,
            recipients=", ".join(recipients),
            purpose=purpose,
        )

        try:
            response = _gemini.generate_content(prompt)
            body     = response.text.strip()
            logger.info("[EmailBodyGenerator] Body generated successfully.")
            return body
        except Exception as exc:
            logger.error(f"[EmailBodyGenerator] Generation failed: {exc}")
            # Minimal fallback body
            return (
                f"Dear recipient,\n\n"
                f"{purpose}\n\n"
                f"--\nVikas Prajapati\nLagozon Technology Pvt. Ltd.\n"
                f"E: vikas.prajapati@lagozon.com | M: +91 9161589883\n"
                f"W: https://www.lagozon.com"
            )


# ═════════════════════════════════════════════════════════════
# THREAD MANAGER
# ═════════════════════════════════════════════════════════════

class ThreadManager:
    """
    Manages email thread records in MongoDB.

    Provides lookup, storage, and retrieval of thread metadata
    to support conversation chaining without repeated Gmail API calls.
    """

    def get_thread(self, thread_id: str) -> dict | None:
        """Retrieve a thread record from MongoDB by thread_id."""
        try:
            return _threads_col.fetch_one({"thread_id": thread_id})
        except Exception as exc:
            logger.error(f"[ThreadManager] get_thread failed: {exc}")
            return None

    def list_threads(
        self,
        limit: int = 20,
        subject_contains: str = "",
    ) -> list[dict]:
        """
        List recent threads, optionally filtered by subject keyword.

        Args:
            limit:            Maximum number of threads to return.
            subject_contains: Case-insensitive subject keyword filter.

        Returns:
            List of thread records from MongoDB.
        """
        try:
            filt: dict = {}
            if subject_contains:
                filt["subject"] = {
                    "$regex": subject_contains, "$options": "i"
                }
            return _threads_col.fetch_all(
                filter=filt,
                sort=[("last_updated", -1)],
                limit=limit,
            )
        except Exception as exc:
            logger.error(f"[ThreadManager] list_threads failed: {exc}")
            return []

    def find_thread_by_subject(self, subject_keyword: str) -> dict | None:
        """
        Find the most recent thread whose subject contains *subject_keyword*.

        Useful when the user says "reply to the invoice thread" without
        providing an explicit thread_id.

        Args:
            subject_keyword: Keyword to search in thread subjects.

        Returns:
            The most recent matching thread record, or None.
        """
        threads = self.list_threads(limit=1, subject_contains=subject_keyword)
        return threads[0] if threads else None

    def get_last_message_id(self, thread_id: str) -> str:
        """
        Return the RFC 2822 Message-ID of the last email in a thread
        from MongoDB, or empty string if not found.

        Used to populate In-Reply-To headers for replies.
        """
        try:
            msgs = _emails_col.fetch_all(
                filter={"thread_id": thread_id},
                sort=[("timestamp", -1)],
                limit=1,
            )
            if msgs:
                return msgs[0].get("gmail_message_id", "")
            return ""
        except Exception as exc:
            logger.error(f"[ThreadManager] get_last_message_id failed: {exc}")
            return ""


# ═════════════════════════════════════════════════════════════
# CONTACT MANAGER
# ═════════════════════════════════════════════════════════════

class ContactManager:
    """
    Manages the contacts collection in MongoDB.

    Contacts are upserted automatically when emails are sent or received.
    Can be queried to resolve names to email addresses.
    """

    def upsert_contact(
        self,
        email: str,
        name: str = "",
        organisation: str = "",
    ) -> None:
        """Add or update a contact record."""
        try:
            _contacts_col.update_one(
                {"email": email.lower().strip()},
                {
                    "$set": {
                        "email":        email.lower().strip(),
                        "name":         name,
                        "organisation": organisation,
                        "updated_at":   datetime.utcnow().isoformat(),
                    },
                    "$setOnInsert": {
                        "created_at": datetime.utcnow().isoformat(),
                    },
                },
                upsert=True,
            )
        except Exception as exc:
            logger.warning(f"[ContactManager] upsert_contact failed: {exc}")

    def find_email_by_name(self, name: str) -> str | None:
        """
        Resolve a person's name to an email address from the contacts DB.

        Args:
            name: Full or partial name to search.

        Returns:
            Email address string, or None if not found.
        """
        try:
            contact = _contacts_col.fetch_one(
                {"name": {"$regex": name, "$options": "i"}}
            )
            return contact["email"] if contact else None
        except Exception as exc:
            logger.warning(f"[ContactManager] find_email_by_name failed: {exc}")
            return None

    def get_all_contacts(self, limit: int = 100) -> list[dict]:
        """Return all contacts, most recently updated first."""
        try:
            return _contacts_col.fetch_all(
                sort=[("updated_at", -1)],
                limit=limit,
            )
        except Exception as exc:
            logger.error(f"[ContactManager] get_all_contacts failed: {exc}")
            return []


# ═════════════════════════════════════════════════════════════
# KNOWLEDGE BASE LOADER
# ═════════════════════════════════════════════════════════════

class KnowledgeBaseLoader:
    """
    Loads organisational knowledge from the knowledge_base/ directory.

    Supports .txt, .md, and .json files. Content is cached in memory
    after the first load and injected into the agent's context.
    """

    _cache: dict[str, str] = {}

    def load_all(self) -> dict[str, str]:
        """
        Load all supported files from KNOWLEDGE_BASE_DIR.

        Returns:
            Dict mapping filename → content string.
        """
        if self._cache:
            return self._cache

        kb_dir = config.KNOWLEDGE_BASE_DIR
        if not kb_dir.exists():
            logger.warning(
                f"[KnowledgeBase] Directory not found: {kb_dir}. "
                "Continuing without knowledge base."
            )
            return {}

        for file_path in kb_dir.iterdir():
            if file_path.suffix.lower() in (".txt", ".md", ".json"):
                try:
                    content = file_path.read_text(encoding="utf-8")
                    self._cache[file_path.name] = content
                    logger.info(
                        f"[KnowledgeBase] Loaded: {file_path.name} "
                        f"({len(content)} chars)"
                    )
                except Exception as exc:
                    logger.warning(
                        f"[KnowledgeBase] Failed to read {file_path.name}: {exc}"
                    )

        logger.info(
            f"[KnowledgeBase] Total files loaded: {len(self._cache)}"
        )
        return self._cache

    def get_combined_context(self) -> str:
        """
        Return all knowledge base content as a single concatenated string,
        suitable for injection into a system prompt or user message.
        """
        docs = self.load_all()
        if not docs:
            return ""
        parts = []
        for name, content in docs.items():
            parts.append(f"--- {name} ---\n{content}")
        return "\n\n".join(parts)

    def find_contact_in_kb(self, name_or_org: str) -> dict | None:
        """
        Search knowledge base text for a contact entry matching
        *name_or_org* and return a parsed dict with 'name', 'email', 'org'.

        This is a best-effort text search — for reliable lookup, use
        ContactManager.find_email_by_name() against MongoDB.
        """
        combined = self.get_combined_context()
        pattern  = re.compile(
            rf"(?i)({re.escape(name_or_org)})[^\n]*?([a-zA-Z0-9_.+\-]+@[a-zA-Z0-9\-]+\.[a-zA-Z0-9\-.]+)"
        )
        match = pattern.search(combined)
        if match:
            return {
                "name":  match.group(1),
                "email": match.group(2),
                "org":   "",
            }
        return None


# ═════════════════════════════════════════════════════════════
# EMAIL WORKFLOW ORCHESTRATOR
# ═════════════════════════════════════════════════════════════

class EmailWorkflow:
    """
    High-level workflow coordinator used by agent.py.

    Chains IntentParser → EmailValidator → body generation → tool execution,
    providing a single entry point for processing natural-language email
    instructions outside the ADK agent loop (e.g. pre-processing or testing).
    """

    def __init__(self) -> None:
        self.intent_parser    = IntentParser()
        self.validator        = EmailValidator()
        self.body_generator   = EmailBodyGenerator()
        self.thread_manager   = ThreadManager()
        self.contact_manager  = ContactManager()
        self.kb_loader        = KnowledgeBaseLoader()

    def parse_and_validate(
        self, user_instruction: str
    ) -> tuple[dict, list[str]]:
        """
        Parse a user instruction and validate the resulting intent.

        Returns:
            (intent, errors) — errors is [] on success.
        """
        intent = self.intent_parser.parse(user_instruction)
        is_valid, errors = self.validator.validate(intent)

        if not is_valid:
            logger.warning(
                f"[EmailWorkflow] Intent validation failed: {errors}"
            )

        return intent, errors

    def enrich_body(self, intent: dict, purpose: str) -> dict:
        """
        If the intent has an empty body, auto-generate one.

        Args:
            intent:  Parsed intent dict.
            purpose: Natural-language description of the email's purpose.

        Returns:
            The intent dict with 'body' populated.
        """
        if not intent.get("body") and intent.get("action") in (
            "send_email", "reply_to_email"
        ):
            logger.info("[EmailWorkflow] Generating email body.")
            recipients = intent.get("to", [])
            intent["body"] = self.body_generator.generate(
                subject=intent.get("subject", ""),
                recipients=recipients,
                purpose=purpose,
            )
        return intent

    def resolve_contacts(self, intent: dict) -> dict:
        """
        Try to resolve any placeholder names in the recipients to real emails
        using the knowledge base and contacts database.

        Args:
            intent: Parsed intent dict.

        Returns:
            The intent dict with resolved email addresses where possible.
        """
        for field in ("to", "cc", "bcc"):
            resolved = []
            for addr in intent.get(field, []):
                if "@" not in addr:
                    # Looks like a name — try to resolve
                    email = self.contact_manager.find_email_by_name(addr)
                    if not email:
                        kb_contact = self.kb_loader.find_contact_in_kb(addr)
                        email = kb_contact["email"] if kb_contact else None

                    if email:
                        logger.info(
                            f"[EmailWorkflow] Resolved '{addr}' → '{email}'"
                        )
                        resolved.append(email)
                    else:
                        logger.warning(
                            f"[EmailWorkflow] Could not resolve name: '{addr}'"
                        )
                        resolved.append(addr)   # keep as-is; validator will catch it
                else:
                    resolved.append(addr)
            intent[field] = resolved
        return intent


# ── Module-level singletons (for import convenience) ─────────
intent_parser   = IntentParser()
email_validator = EmailValidator()
body_generator  = EmailBodyGenerator()
thread_manager  = ThreadManager()
contact_manager = ContactManager()
kb_loader       = KnowledgeBaseLoader()
email_workflow  = EmailWorkflow()
