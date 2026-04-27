# config.py
# Centralised configuration — loads from .env and validates required keys.

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


class Config:
    # ── Models ────────────────────────────────────────────────
    MODEL: str = os.getenv("MODEL", "gemini-2.5-flash")

    # ── Gemini ────────────────────────────────────────────────
    GOOGLE_API_KEY: str = os.getenv("GOOGLE_API_KEY", "")
    if not GOOGLE_API_KEY:
        raise ValueError("GOOGLE_API_KEY is required.")

    # ── GCS ───────────────────────────────────────────────────
    GOOGLE_CSE_ID: str                   = os.getenv("GOOGLE_CSE_ID", "")
    GOOGLE_APPLICATION_CREDENTIALS: str  = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "")
    BUCKET_NAME: str                     = os.getenv("BUCKET_NAME", "")

    if not GOOGLE_APPLICATION_CREDENTIALS:
        raise ValueError("GOOGLE_APPLICATION_CREDENTIALS is required.")
    if not BUCKET_NAME:
        raise ValueError("BUCKET_NAME is required.")

    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = GOOGLE_APPLICATION_CREDENTIALS

    # ── MongoDB ───────────────────────────────────────────────
    MONGO_URI:      str = os.getenv("MONGO_URI", "mongodb://localhost:27017")
    MONGO_DB:       str = os.getenv("MONGO_DB", "jarvis_email_db")
    MONGO_USER:     str = os.getenv("MONGO_USER", "")
    MONGO_PASSWORD: str = os.getenv("MONGO_PASSWORD", "")

    if MONGO_USER and MONGO_PASSWORD:
        MONGO_URI = f"mongodb://{MONGO_USER}:{MONGO_PASSWORD}@localhost:27017"

    # ── Gmail OAuth2 ──────────────────────────────────────────
    # Path to the OAuth2 client credentials JSON downloaded from
    # Google Cloud Console → APIs & Services → Credentials
    GMAIL_CREDENTIALS_FILE: str = os.getenv(
        "GMAIL_CREDENTIALS_FILE", "credentials.json"
    )
    # Auto-generated on first run; stores the access + refresh tokens
    GMAIL_TOKEN_FILE: str = os.getenv("GMAIL_TOKEN_FILE", "token.json")

    # Gmail API OAuth2 scopes required by this agent
    GMAIL_SCOPES: list[str] = [
        "https://www.googleapis.com/auth/gmail.send",
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/gmail.modify",
    ]

    # Google People API scope (for list_contacts tool)
    CONTACTS_SCOPES: list[str] = [
        "https://www.googleapis.com/auth/contacts.readonly",
        "https://www.googleapis.com/auth/contacts.other.readonly",
    ]

    # Combined OAuth2 scopes used for the single shared token file.
    # NOTE: If you had a token.json from before contacts support was added,
    # delete it and re-run to trigger a fresh browser auth flow.
    ALL_OAUTH_SCOPES: list[str] = GMAIL_SCOPES + CONTACTS_SCOPES

    # ── Email Defaults ────────────────────────────────────────
    DEFAULT_SENDER_EMAIL: str = os.getenv(
        "DEFAULT_SENDER_EMAIL", "vikas.prajapati@lagozon.com"
    )
    DEFAULT_SENDER_NAME: str = os.getenv(
        "DEFAULT_SENDER_NAME", "Vikas Prajapati"
    )

    # ── Attachment Storage ────────────────────────────────────
    ATTACHMENT_BASE_DIR: Path = Path(
        os.getenv("ATTACHMENT_DIR", "attachments")
    )
    # Maximum allowed attachment size in megabytes
    MAX_ATTACHMENT_SIZE_MB: int = int(os.getenv("MAX_ATTACHMENT_SIZE_MB", "25"))

    # ── Email Retry Policy ────────────────────────────────────
    EMAIL_MAX_RETRIES: int   = int(os.getenv("EMAIL_MAX_RETRIES", "3"))
    EMAIL_RETRY_DELAY: float = float(os.getenv("EMAIL_RETRY_DELAY", "2.0"))

    # ── Knowledge Base ────────────────────────────────────────
    KNOWLEDGE_BASE_DIR: Path = Path(
        os.getenv("KNOWLEDGE_BASE_DIR", "knowledge_base")
    )

    # ── Email Fetch Defaults ──────────────────────────────────
    EMAIL_FETCH_MAX_RESULTS: int = int(os.getenv("EMAIL_FETCH_MAX_RESULTS", "10"))

    # ── Application ───────────────────────────────────────────
    APP_NAME: str  = "Jarvis"
    USER_ID: str   = "user_001"
    DEBUG: bool    = os.getenv("DEBUG", "false").lower() == "true"

    # ── HITL (Human-in-the-Loop) ──────────────────────────────
    HITL_DB_URL: str = "sqlite:///hitl.db"


config = Config()
