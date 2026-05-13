# tools.py
# Gmail API tool functions — registered directly with the ADK LlmAgent.
# Each function is a self-contained tool the agent can call.
# Authentication is handled by GmailAuthManager (singleton).

from __future__ import annotations

import asyncio
import base64
import json
import mimetypes
import os
import re
import shutil
import sys
import threading
import time
from datetime import datetime
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any

# ─────────────────────────────────────────────────────────────
_AGENT_DIR = Path(__file__).resolve().parent
_ROOT_DIR  = _AGENT_DIR.parent
for _p in (_AGENT_DIR, _ROOT_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
# ─────────────────────────────────────────────────────────────

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

import google.genai as genai

from email_agent.config import config
from email_agent.logger import get_logger
from email_agent.nosql_db import MongoCollection

logger = get_logger(__name__)

# ── MongoDB collections ───────────────────────────────────────
_emails_col       = MongoCollection("emails")
_threads_col      = MongoCollection("threads")
_attachments_col  = MongoCollection("attachments")
_contacts_col     = MongoCollection("contacts")
_sent_att_col     = MongoCollection("sent_attachments")
_received_att_col = MongoCollection("received_attachments")

# ── Send deduplication guard ──────────────────────────────────
_send_lock: threading.Lock = threading.Lock()
_recent_sends: dict[str, float] = {}   # key -> timestamp of last send
_SEND_DEDUP_TTL: int = 30              # seconds — block duplicate sends within this window

# ── Text-readable file extensions ────────────────────────────
_TEXT_EXTENSIONS: frozenset[str] = frozenset({
    ".txt", ".md", ".csv", ".json", ".xml", ".html",
    ".log", ".yaml", ".yml", ".ini", ".cfg", ".rst",
})


def _make_send_key(to: list[str]) -> str:
    """Create a normalised dedup key from the recipient list."""
    return "|".join(sorted(a.lower().strip() for a in to))


# ═════════════════════════════════════════════════════════════
# GMAIL AUTH MANAGER  (singleton)
# ═════════════════════════════════════════════════════════════

class GmailAuthManager:
    """
    Manages OAuth2 authentication for Gmail and Google People APIs.

    Uses a single shared token file covering ALL_OAUTH_SCOPES so both
    Gmail and Contacts API calls share one browser-auth flow.

    NOTE: If token.json was created before contacts support was added,
    delete it so a fresh OAuth2 flow runs with the expanded scope list.
    """

    _instance: "GmailAuthManager | None" = None
    _creds: Credentials | None = None
    _gmail_service: Any = None
    _people_service: Any = None

    def __new__(cls) -> "GmailAuthManager":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    # ── Credential loading / refresh ──────────────────────────

    def _get_credentials(self) -> Credentials:
        """Return valid OAuth2 credentials, refreshing or re-authorising as needed."""
        if self._creds and self._creds.valid:
            return self._creds

        creds: Credentials | None = None
        token_path = Path(config.GMAIL_TOKEN_FILE)
        if not token_path.is_absolute():
            token_path = _AGENT_DIR / token_path

        creds_path = Path(config.GMAIL_CREDENTIALS_FILE)
        if not creds_path.is_absolute():
            creds_path = _AGENT_DIR / creds_path

        logger.debug(f"[Auth] token_path={token_path} | exists={token_path.exists()}")
        logger.debug(f"[Auth] creds_path={creds_path} | exists={creds_path.exists()}")

        if token_path.exists():
            creds = Credentials.from_authorized_user_file(
                str(token_path), config.ALL_OAUTH_SCOPES
            )
            logger.info("[Auth] Loaded existing OAuth2 token.")

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                logger.info("[Auth] Refreshing expired OAuth2 token.")
                creds.refresh(Request())
            else:
                if not creds_path.exists():
                    raise FileNotFoundError(
                        f"Gmail credentials file not found: {creds_path}\n"
                        "Download it from Google Cloud Console -> APIs & Services "
                        "-> Credentials and save as credentials.json."
                    )
                logger.info("[Auth] Starting OAuth2 browser flow (Gmail + Contacts).")
                flow = InstalledAppFlow.from_client_secrets_file(
                    str(creds_path), config.ALL_OAUTH_SCOPES
                )
                creds = flow.run_local_server(port=0)

            token_path.write_text(creds.to_json())
            logger.info(f"[Auth] Token saved to {token_path}")

        self._creds = creds
        return self._creds

    # ── Service accessors (lazily built, cached) ──────────────

    def get_service(self) -> Any:
        """Return an authenticated Gmail API service object."""
        if not self._gmail_service:
            self._gmail_service = build("gmail", "v1", credentials=self._get_credentials())
            logger.info("[Auth] Gmail API service built.")
        return self._gmail_service

    def get_people_service(self) -> Any:
        """Return an authenticated Google People API service object."""
        if not self._people_service:
            self._people_service = build("people", "v1", credentials=self._get_credentials())
            logger.info("[Auth] People API service built.")
        return self._people_service


_auth_manager = GmailAuthManager()


# ═════════════════════════════════════════════════════════════
# SEMANTIC CLASSIFIERS
# ═════════════════════════════════════════════════════════════

def _classify_email(subject: str, snippet: str, category: str) -> bool:
    """
    Ask Gemini whether an email matches a requested semantic category.

    Used by read_emails when semantic_filter is set. Falls back to True
    (include the email) on any API error to avoid silent data loss.

    Args:
        subject:  Email subject header.
        snippet:  Gmail snippet (first ~100 chars of body).
        category: Natural-language category to test against,
                  e.g. 'technical', 'non-technical', 'related to laptop'.

    Returns:
        True if the email belongs to the category, False otherwise.
    """
    prompt = (
        f"You are an email classifier. Does the email below belong to the "
        f"category '{category}'?\n\n"
        f"Subject: {subject}\n"
        f"Preview: {snippet}\n\n"
        "Reply with exactly one word — YES or NO."
    )
    try:
        client   = genai.Client(api_key=config.GOOGLE_API_KEY)
        response = client.models.generate_content(
            model    = "gemini-2.0-flash",
            contents = prompt,
        )
        answer = (response.text or "").strip().upper()
        return answer.startswith("YES")
    except Exception as exc:
        logger.warning(f"[tools] _classify_email error — defaulting include: {exc}")
        return True   # safe default: include rather than silently drop


def _classify_attachment(filename: str, subject: str, category: str) -> bool:
    """
    Ask Gemini whether an attachment belongs to a requested semantic category.

    Used by list_sent_attachments and list_received_attachments when
    semantic_filter is set. Falls back to True on any API error.

    Args:
        filename: The attachment filename, e.g. 'printer_specs.pdf'.
        subject:  Subject of the email the attachment was part of.
        category: Natural-language category, e.g. 'technical', 'laptop related'.

    Returns:
        True if the attachment belongs to the category, False otherwise.
    """
    prompt = (
        f"You are a document classifier. Based only on the filename and email "
        f"subject below, does this attachment belong to the category '{category}'?\n\n"
        f"Filename: {filename}\n"
        f"Email subject: {subject}\n\n"
        "Reply with exactly one word — YES or NO."
    )
    try:
        client   = genai.Client(api_key=config.GOOGLE_API_KEY)
        response = client.models.generate_content(
            model    = "gemini-2.0-flash",
            contents = prompt,
        )
        answer = (response.text or "").strip().upper()
        return answer.startswith("YES")
    except Exception as exc:
        logger.warning(f"[tools] _classify_attachment error — defaulting include: {exc}")
        return True


# ═════════════════════════════════════════════════════════════
# INTERNAL HELPERS
# ═════════════════════════════════════════════════════════════

def _validate_email(address: str) -> bool:
    """Return True if *address* is a syntactically valid email."""
    pattern = r"^[a-zA-Z0-9_.+\-]+@[a-zA-Z0-9\-]+\.[a-zA-Z0-9\-.]+$"
    return bool(re.match(pattern, address.strip()))


def _validate_emails(addresses: list[str]) -> tuple[list[str], list[str]]:
    """Split addresses into (valid, invalid) lists."""
    valid, invalid = [], []
    for addr in addresses:
        (valid if _validate_email(addr) else invalid).append(addr)
    return valid, invalid


def _safe_attachment_path(file_path: str) -> Path:
    """
    Resolve and sanitise an attachment path.
    Allows:
      1. Any absolute path provided explicitly by the user.
      2. Relative paths — resolved against CWD.
    Raises ValueError if the resolved path contains path-traversal
    sequences that escape the filesystem root (sanity check only).
    """
    resolved = Path(file_path).resolve()

    # Guard against null-byte injection or obviously malformed paths
    if "\x00" in file_path:
        raise ValueError(f"Attachment path contains invalid characters: '{file_path}'")

    # Allow absolute user-supplied paths (e.g. D:\docs\file.pdf or /home/user/file.pdf)
    if Path(file_path).is_absolute():
        return resolved

    # For relative paths, allow anything within CWD or the attachments directory
    cwd             = Path.cwd().resolve()
    attachment_base = config.ATTACHMENT_BASE_DIR.resolve()

    is_in_cwd         = str(resolved).startswith(str(cwd))
    is_in_attachments = str(resolved).startswith(str(attachment_base))

    if not (is_in_cwd or is_in_attachments):
        raise ValueError(
            f"Relative attachment path '{file_path}' is outside permitted directories. "
            f"Use an absolute path or place the file inside '{attachment_base}'."
        )

    return resolved


def _check_attachment_size(path: Path) -> None:
    """Raise ValueError if file exceeds MAX_ATTACHMENT_SIZE_MB."""
    size_mb = path.stat().st_size / (1024 * 1024)
    if size_mb > config.MAX_ATTACHMENT_SIZE_MB:
        raise ValueError(
            f"Attachment '{path.name}' is {size_mb:.1f} MB, "
            f"exceeding the {config.MAX_ATTACHMENT_SIZE_MB} MB limit."
        )


def _build_mime_message(
    sender: str,
    to: list[str],
    cc: list[str],
    bcc: list[str],
    subject: str,
    body: str,
    attachment_paths: list[str],
    thread_id: str = "",
    in_reply_to: str = "",
    references: str = "",
) -> MIMEMultipart:
    """Construct a MIME email message with optional attachments."""
    msg = MIMEMultipart()
    msg["From"]    = sender
    msg["To"]      = ", ".join(to)
    msg["Subject"] = subject

    if cc:
        msg["Cc"]  = ", ".join(cc)
    if bcc:
        msg["Bcc"] = ", ".join(bcc)

    # Threading headers (RFC 2822)
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"]  = references

    msg.attach(MIMEText(body, "plain", "utf-8"))

    for file_path in attachment_paths:
        path = _safe_attachment_path(file_path)
        _check_attachment_size(path)

        mime_type, _ = mimetypes.guess_type(str(path))
        if mime_type is None:
            mime_type = "application/octet-stream"
        main_type, sub_type = mime_type.split("/", 1)

        with open(path, "rb") as f:
            part = MIMEBase(main_type, sub_type)
            part.set_payload(f.read())
            encoders.encode_base64(part)
            part.add_header(
                "Content-Disposition", "attachment", filename=path.name
            )
            msg.attach(part)
            logger.info(f"[Tools] Attached file: {path.name} ({mime_type})")

    return msg


def _encode_message(msg: MIMEMultipart) -> dict:
    """Encode a MIME message as a base64url Gmail API body."""
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
    return {"raw": raw}


def _retry(fn, retries: int = None, delay: float = None):
    """
    Retry *fn* up to *retries* times with *delay* seconds between attempts.
    Returns the result of *fn* or raises the last exception.
    """
    retries = retries or config.EMAIL_MAX_RETRIES
    delay   = delay   or config.EMAIL_RETRY_DELAY
    last_exc = None
    for attempt in range(1, retries + 1):
        try:
            return fn()
        except HttpError as exc:
            last_exc = exc
            logger.warning(
                f"[Retry] Attempt {attempt}/{retries} failed: {exc}"
            )
            if attempt < retries:
                time.sleep(delay * attempt)   # exponential back-off
    raise last_exc


def _store_email_record(record: dict) -> str:
    """Persist an email record to MongoDB; return the inserted _id."""
    try:
        inserted_id = _emails_col.insert_one(record)
        logger.debug(f"[DB] Email record stored: {inserted_id}")
        return inserted_id
    except Exception as exc:
        logger.error(f"[DB] Failed to store email record: {exc}")
        return ""


def _upsert_thread(thread_id: str, subject: str, participants: list[str]) -> None:
    """Create or update a thread record in MongoDB."""
    try:
        _threads_col.update_one(
            {"thread_id": thread_id},
            {
                "$set": {
                    "thread_id": thread_id,
                    "subject": subject,
                    "participants": participants,
                    "last_updated": datetime.utcnow().isoformat(),
                },
                "$setOnInsert": {
                    "created_at": datetime.utcnow().isoformat(),
                },
            },
            upsert=True,
        )
    except Exception as exc:
        logger.error(f"[DB] Failed to upsert thread: {exc}")


def _copy_to_sent_attachments(
    attachments: list[str],
    to: list[str],
    cc: list[str],
    subject: str,
    message_id: str,
    thread_id: str,
) -> None:
    """
    Copy each outbound attachment file into SENT_ATTACHMENTS/ and persist
    a metadata record in the sent_attachments MongoDB collection.

    Called automatically after every successful send_email or reply_to_email.
    Failures are logged but never propagated — the email is already delivered.

    Args:
        attachments: List of source file paths used in the outbound email.
        to:          List of primary recipient email addresses.
        cc:          List of CC recipient email addresses.
        subject:     Email subject line.
        message_id:  Gmail message ID returned after sending.
        thread_id:   Gmail thread ID.
    """
    if not attachments:
        return

    sent_dir = config.SENT_ATTACHMENTS_DIR
    sent_dir.mkdir(parents=True, exist_ok=True)

    for file_path in attachments:
        try:
            src = Path(file_path)
            if not src.exists():
                logger.warning(
                    f"[sent_attachments] Source file missing, skipping: {file_path}"
                )
                continue

            timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
            safe_name = re.sub(r"[^\w.\-]", "_", src.name)
            dest      = sent_dir / f"{timestamp}_{safe_name}"

            shutil.copy2(src, dest)

            _sent_att_col.insert_one({
                "filename":       src.name,
                "saved_path":     str(dest),
                "original_path":  str(src),
                "to":             to,
                "cc":             cc,
                "subject":        subject,
                "message_id":     message_id,
                "thread_id":      thread_id,
                "size_bytes":     src.stat().st_size,
                "file_extension": src.suffix.lower(),
                "sent_at":        datetime.utcnow().isoformat(),
            })
            logger.info(f"[sent_attachments] Saved: {dest}")

        except Exception as exc:
            logger.warning(
                f"[sent_attachments] Failed to copy '{file_path}': {exc}"
            )


def _decode_body(payload: dict) -> str:
    """Extract plain-text body from a Gmail message payload."""
    body = ""
    if "parts" in payload:
        for part in payload["parts"]:
            if part.get("mimeType") == "text/plain":
                data = part.get("body", {}).get("data", "")
                if data:
                    body = base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
                    break
        if not body:
            # Fallback: recurse into nested parts
            for part in payload["parts"]:
                body = _decode_body(part)
                if body:
                    break
    else:
        data = payload.get("body", {}).get("data", "")
        if data:
            body = base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
    return body


def _extract_attachment_names(payload: dict) -> list[str]:
    """
    Recursively scan a Gmail message payload and return a list of
    attachment filenames. Works for both simple and multipart messages.
    """
    names: list[str] = []

    def _scan(p: dict) -> None:
        filename  = p.get("filename", "")
        body_size = p.get("body", {}).get("size", 0)
        if filename and body_size > 0:
            names.append(filename)
        for sub in p.get("parts", []):
            _scan(sub)

    _scan(payload)
    return names


def _extract_header(headers: list[dict], name: str) -> str:
    """Extract a specific header value from a Gmail headers list."""
    for h in headers:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


# ═════════════════════════════════════════════════════════════
# TOOL 1 — send_email
# ═════════════════════════════════════════════════════════════

def send_email(
    to: list[str],
    subject: str,
    body: str,
    cc: list[str] | None = None,
    bcc: list[str] | None = None,
    attachments: list[str] | None = None,
    sender: str | None = None,
) -> dict:
    """
    Send a new email via the Gmail API.

    Args:
        to: List of recipient email addresses.
        subject: Email subject line.
        body: Plain-text email body (include signature).
        cc: List of CC recipient email addresses (optional).
        bcc: List of BCC recipient email addresses (optional).
        attachments: List of local file paths to attach (optional).
            Attached files are automatically copied to SENT_ATTACHMENTS/
            and indexed in MongoDB after a successful send.
        sender: Sender address. Defaults to DEFAULT_SENDER_EMAIL.

    Returns:
        dict with keys: success (bool), message_id (str), thread_id (str),
        error (str on failure).
    """
    cc          = cc          or []
    bcc         = bcc         or []
    attachments = attachments or []
    sender      = sender      or config.DEFAULT_SENDER_EMAIL

    logger.info(f"[send_email] to={to} subject='{subject}'")

    # ── Deduplication guard ───────────────────────────────────
    send_key = _make_send_key(to)
    now = time.time()
    with _send_lock:
        last_sent = _recent_sends.get(send_key, 0.0)
        elapsed   = now - last_sent
        if elapsed < _SEND_DEDUP_TTL:
            msg = (
                f"Email to {to} was already sent {int(elapsed)}s ago. "
                f"Duplicate send blocked. The email has been delivered successfully — "
                f"do NOT call send_email again."
            )
            logger.warning(f"[send_email] {msg}")
            return {
                "success":    True,
                "message_id": "",
                "thread_id":  "",
                "error":      "",
                "note":       msg,
            }
        _recent_sends[send_key] = now

    # ── Validate recipients ───────────────────────────────────
    all_recipients = to + cc + bcc
    valid, invalid = _validate_emails(all_recipients)
    if invalid:
        msg = f"Invalid email addresses: {invalid}"
        logger.error(f"[send_email] {msg}")
        return {"success": False, "message_id": "", "thread_id": "", "error": msg}

    if not to:
        return {
            "success": False,
            "message_id": "",
            "thread_id": "",
            "error": "At least one 'to' recipient is required.",
        }

    try:
        # ── Build MIME message ────────────────────────────────
        mime_msg = _build_mime_message(
            sender=sender,
            to=to,
            cc=cc,
            bcc=bcc,
            subject=subject,
            body=body,
            attachment_paths=attachments,
        )
        encoded = _encode_message(mime_msg)

        # ── Send via Gmail API (with retry) ───────────────────
        service = _auth_manager.get_service()

        def _do_send():
            return (
                service.users()
                .messages()
                .send(userId="me", body=encoded)
                .execute()
            )

        result     = _retry(_do_send)
        message_id = result.get("id", "")
        thread_id  = result.get("threadId", "")

        logger.info(
            f"[send_email] Sent | message_id={message_id} thread_id={thread_id}"
        )

        # ── Persist email record ──────────────────────────────
        _store_email_record(
            {
                "message_id":  message_id,
                "thread_id":   thread_id,
                "sender":      sender,
                "to":          to,
                "cc":          cc,
                "bcc":         bcc,
                "subject":     subject,
                "body":        body,
                "attachments": attachments,
                "status":      "sent",
                "timestamp":   datetime.utcnow().isoformat(),
            }
        )
        _upsert_thread(thread_id, subject, [sender] + to + cc)

        # ── Archive sent attachments ──────────────────────────
        if attachments:
            _copy_to_sent_attachments(
                attachments, to, cc, subject, message_id, thread_id
            )

        return {
            "success":    True,
            "message_id": message_id,
            "thread_id":  thread_id,
            "error":      "",
        }

    except (ValueError, FileNotFoundError) as exc:
        logger.error(f"[send_email] Validation error: {exc}")
        return {"success": False, "message_id": "", "thread_id": "", "error": str(exc)}
    except HttpError as exc:
        logger.error(f"[send_email] Gmail API error: {exc}")
        _store_email_record(
            {
                "sender": sender, "to": to, "cc": cc, "bcc": bcc,
                "subject": subject, "body": body,
                "status": "failed",
                "error": str(exc),
                "timestamp": datetime.utcnow().isoformat(),
            }
        )
        return {"success": False, "message_id": "", "thread_id": "", "error": str(exc)}


# ═════════════════════════════════════════════════════════════
# TOOL 2 — read_emails
# ═════════════════════════════════════════════════════════════

def read_emails(
    query: str = "is:unread",
    max_results: int = 10,
    cc_domain: str = "",
    semantic_filter: str = "",
) -> dict:
    """
    List emails from Gmail matching a search query, with optional
    client-side CC-domain filtering and semantic classification.

    Args:
        query: Gmail search query string (e.g. "is:unread", "from:john@acme.com",
               "subject:invoice newer_than:7d", "from:@bechtel.com").
               Defaults to "is:unread".
        max_results: Maximum number of emails to return after all filtering
                     (default 10, hard cap 50).
                     When cc_domain or semantic_filter is active, the tool
                     over-fetches by 5x internally to ensure enough results
                     survive filtering.
        cc_domain: If set (e.g. "@lagozon.com"), only return emails where at
                   least one CC address ends with this domain.
                   Gmail does not support CC-domain wildcards natively —
                   this filter is applied client-side after fetching.
        semantic_filter: If set (e.g. "technical", "non-technical", "laptop"),
                         each email is classified by Gemini before inclusion.
                         Leave empty to skip classification (faster).

    Returns:
        dict with keys:
          - success (bool)
          - emails (list of email summaries)
          - total (int) — count of emails returned
          - filters_applied (list[str]) — which filters were active
          - error (str)
        Each email summary has: message_id, thread_id, subject, sender,
        to, cc, date, snippet, labels, has_attachments (bool),
        attachment_names (list[str]).
    """
    max_results = min(max_results, 50)

    needs_filtering = bool(cc_domain or semantic_filter)
    fetch_n         = min(max_results * 5, 100) if needs_filtering else max_results

    logger.info(
        f"[read_emails] query='{query}' max_results={max_results} "
        f"fetch_n={fetch_n} cc_domain='{cc_domain}' "
        f"semantic_filter='{semantic_filter}'"
    )

    try:
        service = _auth_manager.get_service()

        def _list():
            return (
                service.users()
                .messages()
                .list(userId="me", q=query, maxResults=fetch_n)
                .execute()
            )

        result   = _retry(_list)
        messages = result.get("messages", [])

        if not messages:
            return {
                "success": True, "emails": [], "total": 0,
                "filters_applied": [], "error": "",
            }

        emails: list[dict]         = []
        filters_applied: list[str] = []

        for msg_ref in messages:
            if len(emails) >= max_results:
                break

            msg_id = msg_ref["id"]
            try:
                def _get(mid=msg_id):
                    return (
                        service.users()
                        .messages()
                        # Use "full" so parts (attachments) are included
                        .get(userId="me", id=mid, format="full")
                        .execute()
                    )

                full_msg = _retry(_get)
                payload  = full_msg.get("payload", {})
                headers  = payload.get("headers", [])

                subject = _extract_header(headers, "Subject")
                sender  = _extract_header(headers, "From")
                to_hdr  = _extract_header(headers, "To")
                cc_hdr  = _extract_header(headers, "Cc")
                date    = _extract_header(headers, "Date")
                snippet = full_msg.get("snippet", "")

                # ── CC-domain filter (client-side) ────────────
                if cc_domain:
                    cc_addrs = re.findall(r"[\w.+\-]+@[\w.\-]+", cc_hdr)
                    if not any(
                        addr.lower().endswith(cc_domain.lower())
                        for addr in cc_addrs
                    ):
                        continue
                    if "cc_domain" not in filters_applied:
                        filters_applied.append("cc_domain")

                # ── Semantic filter (Gemini classification) ───
                if semantic_filter:
                    if not _classify_email(subject, snippet, semantic_filter):
                        continue
                    if "semantic" not in filters_applied:
                        filters_applied.append("semantic")

                # ── Attachment detection ──────────────────────
                attachment_names = _extract_attachment_names(payload)

                email_summary = {
                    "message_id":       full_msg.get("id", ""),
                    "thread_id":        full_msg.get("threadId", ""),
                    "subject":          subject,
                    "sender":           sender,
                    "to":               to_hdr,
                    "cc":               cc_hdr,
                    "date":             date,
                    "gmail_message_id": _extract_header(headers, "Message-ID"),
                    "snippet":          snippet,
                    "labels":           full_msg.get("labelIds", []),
                    "has_attachments":  len(attachment_names) > 0,
                    "attachment_names": attachment_names,
                }
                emails.append(email_summary)

                _store_email_record(
                    {
                        **email_summary,
                        "status":    "read",
                        "timestamp": datetime.utcnow().isoformat(),
                    }
                )

            except HttpError as exc:
                logger.warning(f"[read_emails] Could not fetch msg {msg_id}: {exc}")

        logger.info(
            f"[read_emails] Fetched {len(messages)} | "
            f"returned {len(emails)} | filters={filters_applied}"
        )
        return {
            "success":         True,
            "emails":          emails,
            "total":           len(emails),
            "filters_applied": filters_applied,
            "error":           "",
        }

    except HttpError as exc:
        logger.error(f"[read_emails] Gmail API error: {exc}")
        return {
            "success": False, "emails": [], "total": 0,
            "filters_applied": [], "error": str(exc),
        }


# ═════════════════════════════════════════════════════════════
# TOOL 3 — get_thread
# ═════════════════════════════════════════════════════════════

def get_thread(thread_id: str) -> dict:
    """
    Fetch the full email conversation thread by thread_id.

    Retrieves all messages in the thread in chronological order,
    including full body text, threading headers, and attachment names.

    Args:
        thread_id: Gmail thread ID (e.g. "182abc3f4d5e6f7a").

    Returns:
        dict with keys: success (bool), thread_id (str),
        subject (str), messages (list), error (str).
        Each message has: message_id, sender, to, date, body, snippet,
        has_attachments (bool), attachment_names (list[str]).
    """
    logger.info(f"[get_thread] thread_id={thread_id}")

    if not thread_id:
        return {
            "success": False,
            "thread_id": "",
            "subject": "",
            "messages": [],
            "error": "thread_id is required.",
        }

    try:
        service = _auth_manager.get_service()

        def _get():
            return (
                service.users()
                .threads()
                .get(userId="me", id=thread_id, format="full")
                .execute()
            )

        thread    = _retry(_get)
        raw_msgs  = thread.get("messages", [])
        subject   = ""
        messages  = []
        participants = set()

        for raw_msg in raw_msgs:
            payload = raw_msg.get("payload", {})
            headers = payload.get("headers", [])
            body    = _decode_body(payload)
            sender  = _extract_header(headers, "From")
            to_addr = _extract_header(headers, "To")
            subj    = _extract_header(headers, "Subject")

            if not subject and subj:
                subject = subj

            participants.add(sender)
            participants.update(to_addr.split(","))

            attachment_names = _extract_attachment_names(payload)

            msg_record = {
                "message_id":        raw_msg.get("id", ""),
                "thread_id":         thread_id,
                "gmail_message_id":  _extract_header(headers, "Message-ID"),
                "in_reply_to":       _extract_header(headers, "In-Reply-To"),
                "references":        _extract_header(headers, "References"),
                "sender":            sender,
                "to":                to_addr,
                "date":              _extract_header(headers, "Date"),
                "subject":           subj,
                "body":              body,
                "snippet":           raw_msg.get("snippet", ""),
                "labels":            raw_msg.get("labelIds", []),
                "has_attachments":   len(attachment_names) > 0,
                "attachment_names":  attachment_names,
            }
            messages.append(msg_record)

        _upsert_thread(thread_id, subject, list(participants))

        logger.info(
            f"[get_thread] Fetched {len(messages)} message(s) | subject='{subject}'"
        )
        return {
            "success":   True,
            "thread_id": thread_id,
            "subject":   subject,
            "messages":  messages,
            "error":     "",
        }

    except HttpError as exc:
        logger.error(f"[get_thread] Gmail API error: {exc}")
        return {
            "success":   False,
            "thread_id": thread_id,
            "subject":   "",
            "messages":  [],
            "error":     str(exc),
        }


# ═════════════════════════════════════════════════════════════
# TOOL 4 — reply_to_email
# ═════════════════════════════════════════════════════════════

def reply_to_email(
    thread_id: str,
    body: str,
    message_id: str = "",
    to: list[str] | None = None,
    cc: list[str] | None = None,
    attachments: list[str] | None = None,
    sender: str | None = None,
) -> dict:
    """
    Send a reply within an existing Gmail thread, preserving threading headers.

    This correctly sets In-Reply-To and References headers so the reply
    appears inline within the thread in all email clients.

    Args:
        thread_id: Gmail thread ID to reply in.
        body: Reply body text (include signature).
        message_id: The RFC 2822 Message-ID of the message being replied to.
                    Fetch via get_thread if unknown.
        to: Override recipients. If omitted, replies to the original sender.
        cc: CC recipients (optional).
        attachments: Local file paths to attach (optional).
            Attached files are automatically copied to SENT_ATTACHMENTS/
            and indexed in MongoDB after a successful send.
        sender: Sender email address. Defaults to DEFAULT_SENDER_EMAIL.

    Returns:
        dict with keys: success (bool), message_id (str), thread_id (str), error (str).
    """
    cc          = cc          or []
    attachments = attachments or []
    sender      = sender      or config.DEFAULT_SENDER_EMAIL

    logger.info(f"[reply_to_email] thread_id={thread_id}")

    if not thread_id:
        return {
            "success": False, "message_id": "", "thread_id": "",
            "error": "thread_id is required for replies.",
        }

    try:
        service = _auth_manager.get_service()

        # ── Fetch thread to get subject and last message headers ──
        thread_data = get_thread(thread_id)
        if not thread_data["success"] or not thread_data["messages"]:
            return {
                "success": False, "message_id": "", "thread_id": thread_id,
                "error": f"Could not fetch thread {thread_id}.",
            }

        thread_msgs = thread_data["messages"]
        last_msg    = thread_msgs[-1]

        subject = thread_data["subject"]
        if not subject.lower().startswith("re:"):
            subject = f"Re: {subject}"

        # Use provided message_id or fall back to last message
        in_reply_to = message_id or last_msg.get("gmail_message_id", "")
        references  = last_msg.get("references", "")
        if in_reply_to:
            references = f"{references} {in_reply_to}".strip()

        # Default reply-to: original sender
        if not to:
            original_sender = last_msg.get("sender", "")
            match = re.search(r"<(.+?)>", original_sender)
            to = [match.group(1) if match else original_sender]

        # Validate
        valid, invalid = _validate_emails(to + cc)
        if invalid:
            return {
                "success": False, "message_id": "", "thread_id": thread_id,
                "error": f"Invalid email addresses: {invalid}",
            }

        # ── Build MIME message ────────────────────────────────
        mime_msg = _build_mime_message(
            sender=sender,
            to=to,
            cc=cc,
            bcc=[],
            subject=subject,
            body=body,
            attachment_paths=attachments,
            thread_id=thread_id,
            in_reply_to=in_reply_to,
            references=references,
        )
        encoded = _encode_message(mime_msg)
        encoded["threadId"] = thread_id   # attach to existing Gmail thread

        def _do_send():
            return (
                service.users()
                .messages()
                .send(userId="me", body=encoded)
                .execute()
            )

        result     = _retry(_do_send)
        new_msg_id = result.get("id", "")
        logger.info(
            f"[reply_to_email] Reply sent | message_id={new_msg_id} thread_id={thread_id}"
        )

        # ── Persist email record ──────────────────────────────
        _store_email_record(
            {
                "message_id":  new_msg_id,
                "thread_id":   thread_id,
                "sender":      sender,
                "to":          to,
                "cc":          cc,
                "subject":     subject,
                "body":        body,
                "attachments": attachments,
                "in_reply_to": in_reply_to,
                "status":      "replied",
                "timestamp":   datetime.utcnow().isoformat(),
            }
        )
        _upsert_thread(thread_id, subject, [sender] + to + cc)

        # ── Archive sent attachments ──────────────────────────
        if attachments:
            _copy_to_sent_attachments(
                attachments, to, cc, subject, new_msg_id, thread_id
            )

        return {
            "success":    True,
            "message_id": new_msg_id,
            "thread_id":  thread_id,
            "error":      "",
        }

    except (ValueError, FileNotFoundError) as exc:
        logger.error(f"[reply_to_email] Validation error: {exc}")
        return {"success": False, "message_id": "", "thread_id": thread_id, "error": str(exc)}
    except HttpError as exc:
        logger.error(f"[reply_to_email] Gmail API error: {exc}")
        return {"success": False, "message_id": "", "thread_id": thread_id, "error": str(exc)}


# ═════════════════════════════════════════════════════════════
# TOOL 5 — download_attachments
# ═════════════════════════════════════════════════════════════

def download_attachments(
    query: str = "",
    thread_id: str = "",
    message_id: str = "",
    download_dir: str = "",
) -> dict:
    """
    Download all attachments from matching emails to structured local storage.

    Storage layout:
        attachments/{thread_id}/{timestamp}_{filename}

    Provide at least one of: query, thread_id, or message_id.

    Args:
        query: Gmail search query to find emails with attachments.
               E.g. "subject:invoice has:attachment".
        thread_id: Download attachments from a specific thread.
        message_id: Download attachments from a specific message.
        download_dir: Override base download directory (optional).

    Returns:
        dict with keys: success (bool), downloaded (list of file paths),
        count (int), error (str).
    """
    logger.info(
        f"[download_attachments] query='{query}' thread_id={thread_id} "
        f"message_id={message_id}"
    )

    if not any([query, thread_id, message_id]):
        return {
            "success": False, "downloaded": [], "count": 0,
            "error": "Provide at least one of: query, thread_id, message_id.",
        }

    base_dir = Path(download_dir) if download_dir else config.ATTACHMENT_BASE_DIR
    base_dir.mkdir(parents=True, exist_ok=True)

    try:
        service    = _auth_manager.get_service()
        msg_ids: list[str] = []

        # ── Resolve message IDs ───────────────────────────────
        if message_id:
            msg_ids = [message_id]

        elif thread_id:
            thread_data = get_thread(thread_id)
            msg_ids = [m["message_id"] for m in thread_data.get("messages", [])]

        elif query:
            if "has:attachment" not in query:
                query += " has:attachment"

            def _list():
                return (
                    service.users()
                    .messages()
                    .list(userId="me", q=query, maxResults=20)
                    .execute()
                )

            result  = _retry(_list)
            msg_ids = [m["id"] for m in result.get("messages", [])]

        downloaded = []

        for mid in msg_ids:
            try:
                def _get(m=mid):
                    return (
                        service.users()
                        .messages()
                        .get(userId="me", id=m, format="full")
                        .execute()
                    )

                full_msg   = _retry(_get)
                raw_thread = full_msg.get("threadId", mid)
                payload    = full_msg.get("payload", {})
                parts      = payload.get("parts", [])

                def _process_parts(parts_list: list) -> None:
                    for part in parts_list:
                        if part.get("parts"):
                            _process_parts(part["parts"])

                        filename = part.get("filename", "")
                        body     = part.get("body", {})
                        att_id   = body.get("attachmentId")

                        if not filename or not att_id:
                            continue

                        def _fetch_att(m=mid, a=att_id):
                            return (
                                service.users()
                                .messages()
                                .attachments()
                                .get(userId="me", messageId=m, id=a)
                                .execute()
                            )

                        att_data  = _retry(_fetch_att)
                        file_data = base64.urlsafe_b64decode(
                            att_data.get("data", "")
                        )

                        size_mb = len(file_data) / (1024 * 1024)
                        if size_mb > config.MAX_ATTACHMENT_SIZE_MB:
                            logger.warning(
                                f"[download_attachments] Skipping {filename}: "
                                f"{size_mb:.1f} MB exceeds limit."
                            )
                            continue

                        thread_dir = base_dir / raw_thread
                        thread_dir.mkdir(parents=True, exist_ok=True)
                        timestamp  = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
                        safe_name  = re.sub(r"[^\w.\-]", "_", filename)
                        dest       = thread_dir / f"{timestamp}_{safe_name}"

                        dest.write_bytes(file_data)
                        logger.info(
                            f"[download_attachments] Saved: {dest} ({size_mb:.2f} MB)"
                        )

                        try:
                            _attachments_col.insert_one(
                                {
                                    "message_id":    mid,
                                    "thread_id":     raw_thread,
                                    "filename":      filename,
                                    "size_bytes":    len(file_data),
                                    "local_path":    str(dest),
                                    "downloaded_at": datetime.utcnow().isoformat(),
                                }
                            )
                        except Exception as db_exc:
                            logger.warning(f"[DB] attachment record failed: {db_exc}")

                        downloaded.append(str(dest))

                _process_parts(parts)

            except HttpError as exc:
                logger.warning(f"[download_attachments] Failed for msg {mid}: {exc}")

        logger.info(f"[download_attachments] Total downloaded: {len(downloaded)}")
        return {
            "success":    True,
            "downloaded": downloaded,
            "count":      len(downloaded),
            "error":      "",
        }

    except HttpError as exc:
        logger.error(f"[download_attachments] Gmail API error: {exc}")
        return {"success": False, "downloaded": [], "count": 0, "error": str(exc)}


# ═════════════════════════════════════════════════════════════
# TOOL 6 — attach_files
# ═════════════════════════════════════════════════════════════

def attach_files(file_paths: list[str]) -> dict:
    """
    Validate and prepare local files for attachment in an outgoing email.

    This tool validates each file — checking existence, size limits, and
    path safety — and returns a cleaned list ready to pass to send_email
    or reply_to_email as the 'attachments' parameter.

    Args:
        file_paths: List of absolute or relative local file paths to validate.

    Returns:
        dict with keys:
          - success (bool)
          - valid_paths (list[str])  — safe, validated paths
          - invalid (list[dict])     — {path, reason} for each rejected file
          - error (str)
    """
    logger.info(f"[attach_files] Validating {len(file_paths)} file(s).")

    valid_paths: list[str] = []
    invalid: list[dict]    = []

    for fp in file_paths:
        try:
            path = _safe_attachment_path(fp)

            if not path.exists():
                invalid.append({"path": fp, "reason": "File not found."})
                continue

            if not path.is_file():
                invalid.append({"path": fp, "reason": "Path is not a file."})
                continue

            _check_attachment_size(path)

            valid_paths.append(str(path))
            logger.info(
                f"[attach_files] Valid: {path.name} "
                f"({path.stat().st_size / 1024:.1f} KB)"
            )

        except ValueError as exc:
            invalid.append({"path": fp, "reason": str(exc)})
        except Exception as exc:
            invalid.append({"path": fp, "reason": f"Unexpected error: {exc}"})

    all_ok = len(invalid) == 0
    logger.info(
        f"[attach_files] Result: {len(valid_paths)} valid, {len(invalid)} invalid."
    )

    return {
        "success":     all_ok,
        "valid_paths": valid_paths,
        "invalid":     invalid,
        "error":       "" if all_ok else f"{len(invalid)} file(s) failed validation.",
    }


# ═════════════════════════════════════════════════════════════
# TOOL 7 — list_contacts
# ═════════════════════════════════════════════════════════════

def list_contacts(
    query: str = "",
    max_results: int = 50,
) -> dict:
    """
    List known email addresses from Google Contacts (People API).

    Searches both "My Contacts" (explicit saves) and "Other Contacts"
    (auto-saved from email history). Returns all contacts that have at
    least one email address.

    Args:
        query: Optional search string to filter contacts by name or email
               (case-insensitive). Leave empty to list all contacts.
        max_results: Maximum number of contacts to return (default 50, max 1000).

    Returns:
        dict with keys:
          - success (bool)
          - contacts (list) — each entry has: name (str), emails (list[str]),
            phones (list[str]), source (str: 'contacts' | 'other_contacts')
          - total (int)
          - error (str)
    """
    max_results = min(max_results, 1000)
    logger.info(f"[list_contacts] query='{query}' max_results={max_results}")

    try:
        service = _auth_manager.get_people_service()
        q_lower = query.lower() if query else ""

        def _parse_person(person: dict, source: str) -> dict | None:
            names  = person.get("names", [])
            emails = person.get("emailAddresses", [])
            phones = person.get("phoneNumbers", [])

            if not emails:
                return None

            name   = names[0].get("displayName", "") if names else ""
            e_list = [e["value"] for e in emails if e.get("value")]
            p_list = [p["value"] for p in phones if p.get("value")]

            if not e_list:
                return None

            if q_lower:
                name_match  = q_lower in name.lower()
                email_match = any(q_lower in e.lower() for e in e_list)
                if not name_match and not email_match:
                    return None

            return {"name": name, "emails": e_list, "phones": p_list, "source": source}

        contacts: list[dict] = []
        seen_emails: set[str] = set()

        # ── SOURCE 1: "My Contacts" (people.connections) ──────
        try:
            next_page_token = ""
            while len(contacts) < max_results:
                kwargs: dict = {
                    "resourceName": "people/me",
                    "pageSize":     min(max_results, 1000),
                    "personFields": "names,emailAddresses,phoneNumbers",
                }
                if next_page_token:
                    kwargs["pageToken"] = next_page_token

                def _list_connections(kw=kwargs):
                    return service.people().connections().list(**kw).execute()

                response      = _retry(_list_connections)
                page_contacts = response.get("connections", [])

                for person in page_contacts:
                    if len(contacts) >= max_results:
                        break
                    parsed = _parse_person(person, "contacts")
                    if parsed:
                        primary = parsed["emails"][0].lower()
                        if primary not in seen_emails:
                            seen_emails.add(primary)
                            contacts.append(parsed)

                next_page_token = response.get("nextPageToken", "")
                if not next_page_token:
                    break

        except Exception as exc:
            logger.warning(f"[list_contacts] connections.list failed: {exc}")

        # ── SOURCE 2: "Other Contacts" (otherContacts) ────────
        try:
            next_page_token = ""
            while len(contacts) < max_results:
                kwargs2: dict = {
                    "pageSize": min(max_results, 1000),
                    "readMask": "names,emailAddresses,phoneNumbers",
                }
                if next_page_token:
                    kwargs2["pageToken"] = next_page_token

                def _list_other(kw=kwargs2):
                    return service.otherContacts().list(**kw).execute()

                response      = _retry(_list_other)
                page_contacts = response.get("otherContacts", [])

                for person in page_contacts:
                    if len(contacts) >= max_results:
                        break
                    parsed = _parse_person(person, "other_contacts")
                    if parsed:
                        primary = parsed["emails"][0].lower()
                        if primary not in seen_emails:
                            seen_emails.add(primary)
                            contacts.append(parsed)

                next_page_token = response.get("nextPageToken", "")
                if not next_page_token:
                    break

        except Exception as exc:
            logger.warning(f"[list_contacts] otherContacts.list failed: {exc}")

        # ── Persist to MongoDB (upsert by primary email) ──────
        for contact in contacts:
            if contact["emails"]:
                try:
                    _contacts_col.update_one(
                        {"email": contact["emails"][0]},
                        {
                            "$set": {
                                "email":      contact["emails"][0],
                                "all_emails": contact["emails"],
                                "name":       contact["name"],
                                "phones":     contact["phones"],
                                "source":     contact["source"],
                                "synced_at":  datetime.utcnow().isoformat(),
                            }
                        },
                        upsert=True,
                    )
                except Exception as db_exc:
                    logger.warning(f"[list_contacts] DB upsert failed: {db_exc}")

        logger.info(f"[list_contacts] Returned {len(contacts)} contact(s).")
        return {
            "success":  True,
            "contacts": contacts,
            "total":    len(contacts),
            "error":    "",
        }

    except HttpError as exc:
        logger.error(f"[list_contacts] People API error: {exc}")
        return {"success": False, "contacts": [], "total": 0, "error": str(exc)}


# ═════════════════════════════════════════════════════════════
# TOOL 8 — list_sent_attachments
# ═════════════════════════════════════════════════════════════

def list_sent_attachments(
    semantic_filter: str = "",
    recipient_filter: str = "",
    filename_filter: str = "",
    limit: int = 50,
) -> dict:
    """
    List attachments that were sent in outbound emails, with optional filters.

    Records are created automatically every time send_email or reply_to_email
    succeeds with attachments. Each record includes the filename, saved path,
    recipients, email subject, message/thread IDs, and timestamp.

    Use this tool to answer queries such as:
      - "List all sent attachments"
      - "How many technical attachments have I sent?"
      - "Show sent docs related to laptop"
      - "To which vendors did I send a printer attachment?"

    Args:
        semantic_filter: Natural-language category to classify attachments by
                         (e.g. "technical", "related to laptop", "quotation").
                         Classification is performed per-file by Gemini using
                         the filename and email subject as context.
                         Leave empty to skip classification.
        recipient_filter: Case-insensitive substring matched against recipient
                          email addresses in the 'to' field
                          (e.g. "vendor.com", "john@acme.com").
                          Leave empty to return all recipients.
        filename_filter: Case-insensitive substring matched against the filename
                         (e.g. "printer", "invoice", ".pdf").
                         Leave empty to return all filenames.
        limit: Maximum number of records to return (default 50).

    Returns:
        dict with keys:
          - success (bool)
          - attachments (list) — each record has: _id, filename, saved_path,
            original_path, to (list), cc (list), subject, message_id,
            thread_id, size_bytes, file_extension, sent_at
          - total (int)
          - filters_applied (list[str])
          - error (str)
    """
    logger.info(
        f"[list_sent_attachments] semantic='{semantic_filter}' "
        f"recipient='{recipient_filter}' filename='{filename_filter}' limit={limit}"
    )

    try:
        mongo_filter: dict = {}

        if recipient_filter:
            mongo_filter["to"] = {
                "$elemMatch": {"$regex": recipient_filter, "$options": "i"}
            }

        if filename_filter:
            mongo_filter["filename"] = {"$regex": filename_filter, "$options": "i"}

        records = _sent_att_col.fetch_all(
            filter=mongo_filter,
            sort=[("sent_at", -1)],
            limit=limit,
        )

        filters_applied: list[str] = []

        if recipient_filter:
            filters_applied.append("recipient")
        if filename_filter:
            filters_applied.append("filename")

        # Semantic classification runs client-side via Gemini
        if semantic_filter and records:
            filters_applied.append("semantic")
            records = [
                r for r in records
                if _classify_attachment(
                    r.get("filename", ""),
                    r.get("subject", ""),
                    semantic_filter,
                )
            ]

        logger.info(f"[list_sent_attachments] Returning {len(records)} record(s).")
        return {
            "success":          True,
            "attachments":      records,
            "total":            len(records),
            "filters_applied":  filters_applied,
            "error":            "",
        }

    except Exception as exc:
        logger.error(f"[list_sent_attachments] Error: {exc}")
        return {
            "success": False, "attachments": [], "total": 0,
            "filters_applied": [], "error": str(exc),
        }


# ═════════════════════════════════════════════════════════════
# TOOL 9 — manage_sent_attachment
# ═════════════════════════════════════════════════════════════

def manage_sent_attachment(
    action: str,
    doc_id: str,
    new_file_path: str = "",
) -> dict:
    """
    Perform a read, delete, or update operation on a single sent attachment.

    Use doc_id from the _id field returned by list_sent_attachments.

    Args:
        action: Operation to perform. Must be one of:
            read   - Return file content for text files, or metadata for
                     binary files (PDF, DOCX, XLSX, images, etc.).
            delete - Remove the local file from SENT_ATTACHMENTS/ and delete
                     the MongoDB record.
            update - Replace the stored file with a new file. The MongoDB
                     record is updated with the new filename and size.
        doc_id: MongoDB _id string of the sent attachment record.
        new_file_path: Required only for action='update'. Absolute or relative
                       path to the replacement file.

    Returns:
        dict with keys: success (bool), content or message (str), error (str).
        For action='read': also includes metadata (dict) with the full record.
    """
    logger.info(f"[manage_sent_attachment] action='{action}' doc_id='{doc_id}'")

    try:
        record = _sent_att_col.fetch_by_id(doc_id)
        if not record:
            return {
                "success": False,
                "error": f"No sent attachment found with id: {doc_id}",
            }

        saved_path = Path(record.get("saved_path", ""))

        if action == "read":
            if not saved_path.exists():
                return {
                    "success": False,
                    "error": f"File not found on disk: {saved_path}",
                }
            ext = saved_path.suffix.lower()
            if ext in _TEXT_EXTENSIONS:
                content = saved_path.read_text(encoding="utf-8", errors="replace")
                return {
                    "success":  True,
                    "content":  content,
                    "metadata": record,
                    "error":    "",
                }
            return {
                "success":  True,
                "content":  f"Binary file ({ext}) — cannot display inline.",
                "metadata": record,
                "error":    "",
            }

        elif action == "delete":
            if saved_path.exists():
                saved_path.unlink()
                logger.info(f"[manage_sent_attachment] Deleted file: {saved_path}")
            _sent_att_col.delete_by_id(doc_id)
            return {
                "success": True,
                "message": f"Deleted: {record.get('filename')}",
                "error":   "",
            }

        elif action == "update":
            if not new_file_path:
                return {
                    "success": False,
                    "error": "new_file_path is required for action='update'.",
                }
            new_src = Path(new_file_path)
            if not new_src.exists():
                return {
                    "success": False,
                    "error": f"Replacement file not found: {new_file_path}",
                }
            shutil.copy2(new_src, saved_path)
            _sent_att_col.update_by_id(
                doc_id,
                {
                    "filename":      new_src.name,
                    "original_path": str(new_src),
                    "size_bytes":    new_src.stat().st_size,
                    "file_extension": new_src.suffix.lower(),
                    "updated_at":    datetime.utcnow().isoformat(),
                },
            )
            logger.info(
                f"[manage_sent_attachment] Updated doc_id={doc_id} -> {new_src.name}"
            )
            return {
                "success": True,
                "message": f"Updated attachment to: {new_src.name}",
                "error":   "",
            }

        else:
            return {
                "success": False,
                "error": f"Unknown action: '{action}'. Valid values: read, delete, update.",
            }

    except Exception as exc:
        logger.error(f"[manage_sent_attachment] Error: {exc}")
        return {"success": False, "error": str(exc)}


# ═════════════════════════════════════════════════════════════
# TOOL 10 — save_received_attachments
# ═════════════════════════════════════════════════════════════

def save_received_attachments(
    message_id: str,
    thread_id: str = "",
) -> dict:
    """
    Explicitly save attachments from a received Gmail message to
    RECEIVED_ATTACHMENTS/ and index them in MongoDB.

    Call this tool only when the user explicitly requests to save attachments
    from a received email (e.g. "save the attachment from this email",
    "store the documents I received from vendor X").

    Each saved file is stored at:
        RECEIVED_ATTACHMENTS/{thread_id}/{timestamp}_{filename}

    A metadata record is inserted into the received_attachments collection
    with the sender, subject, message/thread IDs, local path, and timestamp.

    Args:
        message_id: Gmail message ID of the email whose attachments to save.
                    Obtain this from read_emails or get_thread results.
        thread_id:  Gmail thread ID (optional, used for folder organisation).
                    If omitted, the message's own threadId is used.

    Returns:
        dict with keys:
          - success (bool)
          - saved (list of dicts) — each has: filename, saved_path, size_bytes
          - count (int)
          - error (str)
    """
    logger.info(
        f"[save_received_attachments] message_id={message_id} thread_id={thread_id}"
    )

    if not message_id:
        return {
            "success": False, "saved": [], "count": 0,
            "error": "message_id is required.",
        }

    try:
        service = _auth_manager.get_service()

        def _get_msg():
            return (
                service.users()
                .messages()
                .get(userId="me", id=message_id, format="full")
                .execute()
            )

        full_msg   = _retry(_get_msg)
        payload    = full_msg.get("payload", {})
        headers    = payload.get("headers", [])
        raw_thread = thread_id or full_msg.get("threadId", message_id)

        sender  = _extract_header(headers, "From")
        subject = _extract_header(headers, "Subject")
        date    = _extract_header(headers, "Date")

        recv_dir = config.RECEIVED_ATTACHMENTS_DIR / raw_thread
        recv_dir.mkdir(parents=True, exist_ok=True)

        saved: list[dict] = []

        def _process_parts(parts_list: list) -> None:
            for part in parts_list:
                if part.get("parts"):
                    _process_parts(part["parts"])

                filename = part.get("filename", "")
                body     = part.get("body", {})
                att_id   = body.get("attachmentId")

                if not filename or not att_id:
                    continue

                def _fetch_att(m=message_id, a=att_id):
                    return (
                        service.users()
                        .messages()
                        .attachments()
                        .get(userId="me", messageId=m, id=a)
                        .execute()
                    )

                att_data  = _retry(_fetch_att)
                file_data = base64.urlsafe_b64decode(att_data.get("data", ""))

                size_bytes = len(file_data)
                size_mb    = size_bytes / (1024 * 1024)
                if size_mb > config.MAX_ATTACHMENT_SIZE_MB:
                    logger.warning(
                        f"[save_received_attachments] Skipping {filename}: "
                        f"{size_mb:.1f} MB exceeds limit."
                    )
                    continue

                timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
                safe_name = re.sub(r"[^\w.\-]", "_", filename)
                dest      = recv_dir / f"{timestamp}_{safe_name}"

                dest.write_bytes(file_data)
                logger.info(
                    f"[save_received_attachments] Saved: {dest} ({size_mb:.2f} MB)"
                )

                try:
                    _received_att_col.insert_one({
                        "filename":       filename,
                        "saved_path":     str(dest),
                        "from_email":     sender,
                        "subject":        subject,
                        "email_date":     date,
                        "message_id":     message_id,
                        "thread_id":      raw_thread,
                        "size_bytes":     size_bytes,
                        "file_extension": Path(filename).suffix.lower(),
                        "received_at":    datetime.utcnow().isoformat(),
                    })
                except Exception as db_exc:
                    logger.warning(
                        f"[save_received_attachments] DB insert failed: {db_exc}"
                    )

                saved.append({
                    "filename":   filename,
                    "saved_path": str(dest),
                    "size_bytes": size_bytes,
                })

        _process_parts(payload.get("parts", []))

        if not saved:
            return {
                "success": True,
                "saved":   [],
                "count":   0,
                "error":   "No attachments found in the specified message.",
            }

        logger.info(
            f"[save_received_attachments] Saved {len(saved)} file(s) from {message_id}."
        )
        return {
            "success": True,
            "saved":   saved,
            "count":   len(saved),
            "error":   "",
        }

    except HttpError as exc:
        logger.error(f"[save_received_attachments] Gmail API error: {exc}")
        return {"success": False, "saved": [], "count": 0, "error": str(exc)}
    except Exception as exc:
        logger.error(f"[save_received_attachments] Unexpected error: {exc}")
        return {"success": False, "saved": [], "count": 0, "error": str(exc)}


# ═════════════════════════════════════════════════════════════
# TOOL 11 — list_received_attachments
# ═════════════════════════════════════════════════════════════

def list_received_attachments(
    semantic_filter: str = "",
    sender_filter: str = "",
    filename_filter: str = "",
    limit: int = 50,
) -> dict:
    """
    List attachments received in inbound emails that have been explicitly
    saved via save_received_attachments, with optional filters.

    Use this tool to answer queries such as:
      - "List all received attachments"
      - "How many technical documents have I received?"
      - "Show received docs related to laptop"
      - "From which vendors have I received documents?"
      - "What documents did I receive from vendor@acme.com?"

    Args:
        semantic_filter: Natural-language category to classify attachments by
                         (e.g. "technical", "related to laptop", "invoice").
                         Gemini classifies each file using filename + subject.
                         Leave empty to skip classification.
        sender_filter: Case-insensitive substring matched against the sender's
                       email address or display name
                       (e.g. "vendor.com", "john@acme.com", "Acme").
                       Leave empty to return all senders.
        filename_filter: Case-insensitive substring matched against the filename
                         (e.g. "laptop", "invoice", ".pdf").
                         Leave empty to return all filenames.
        limit: Maximum number of records to return (default 50).

    Returns:
        dict with keys:
          - success (bool)
          - attachments (list) — each record has: _id, filename, saved_path,
            from_email, subject, email_date, message_id, thread_id,
            size_bytes, file_extension, received_at
          - total (int)
          - filters_applied (list[str])
          - error (str)
    """
    logger.info(
        f"[list_received_attachments] semantic='{semantic_filter}' "
        f"sender='{sender_filter}' filename='{filename_filter}' limit={limit}"
    )

    try:
        mongo_filter: dict = {}

        if sender_filter:
            mongo_filter["from_email"] = {"$regex": sender_filter, "$options": "i"}

        if filename_filter:
            mongo_filter["filename"] = {"$regex": filename_filter, "$options": "i"}

        records = _received_att_col.fetch_all(
            filter=mongo_filter,
            sort=[("received_at", -1)],
            limit=limit,
        )

        filters_applied: list[str] = []

        if sender_filter:
            filters_applied.append("sender")
        if filename_filter:
            filters_applied.append("filename")

        if semantic_filter and records:
            filters_applied.append("semantic")
            records = [
                r for r in records
                if _classify_attachment(
                    r.get("filename", ""),
                    r.get("subject", ""),
                    semantic_filter,
                )
            ]

        logger.info(f"[list_received_attachments] Returning {len(records)} record(s).")
        return {
            "success":         True,
            "attachments":     records,
            "total":           len(records),
            "filters_applied": filters_applied,
            "error":           "",
        }

    except Exception as exc:
        logger.error(f"[list_received_attachments] Error: {exc}")
        return {
            "success": False, "attachments": [], "total": 0,
            "filters_applied": [], "error": str(exc),
        }


# ═════════════════════════════════════════════════════════════
# TOOL 12 — manage_received_attachment
# ═════════════════════════════════════════════════════════════

def manage_received_attachment(
    action: str,
    doc_id: str,
    new_file_path: str = "",
) -> dict:
    """
    Perform a read, delete, or update operation on a single received attachment.

    Use doc_id from the _id field returned by list_received_attachments.

    Args:
        action: Operation to perform. Must be one of:
            read   - Return file content for text files, or metadata for
                     binary files (PDF, DOCX, XLSX, images, etc.).
            delete - Remove the local file from RECEIVED_ATTACHMENTS/ and
                     delete the MongoDB record.
            update - Replace the stored file with a new file at new_file_path.
                     The MongoDB record is updated with the new filename and size.
        doc_id: MongoDB _id string of the received attachment record.
        new_file_path: Required only for action='update'. Absolute or relative
                       path to the replacement file.

    Returns:
        dict with keys: success (bool), content or message (str), error (str).
        For action='read': also includes metadata (dict) with the full record.
    """
    logger.info(f"[manage_received_attachment] action='{action}' doc_id='{doc_id}'")

    try:
        record = _received_att_col.fetch_by_id(doc_id)
        if not record:
            return {
                "success": False,
                "error": f"No received attachment found with id: {doc_id}",
            }

        saved_path = Path(record.get("saved_path", ""))

        if action == "read":
            if not saved_path.exists():
                return {
                    "success": False,
                    "error": f"File not found on disk: {saved_path}",
                }
            ext = saved_path.suffix.lower()
            if ext in _TEXT_EXTENSIONS:
                content = saved_path.read_text(encoding="utf-8", errors="replace")
                return {
                    "success":  True,
                    "content":  content,
                    "metadata": record,
                    "error":    "",
                }
            return {
                "success":  True,
                "content":  f"Binary file ({ext}) — cannot display inline.",
                "metadata": record,
                "error":    "",
            }

        elif action == "delete":
            if saved_path.exists():
                saved_path.unlink()
                logger.info(f"[manage_received_attachment] Deleted file: {saved_path}")
            _received_att_col.delete_by_id(doc_id)
            return {
                "success": True,
                "message": f"Deleted: {record.get('filename')}",
                "error":   "",
            }

        elif action == "update":
            if not new_file_path:
                return {
                    "success": False,
                    "error": "new_file_path is required for action='update'.",
                }
            new_src = Path(new_file_path)
            if not new_src.exists():
                return {
                    "success": False,
                    "error": f"Replacement file not found: {new_file_path}",
                }
            shutil.copy2(new_src, saved_path)
            _received_att_col.update_by_id(
                doc_id,
                {
                    "filename":       new_src.name,
                    "size_bytes":     new_src.stat().st_size,
                    "file_extension": new_src.suffix.lower(),
                    "updated_at":     datetime.utcnow().isoformat(),
                },
            )
            logger.info(
                f"[manage_received_attachment] Updated doc_id={doc_id} -> {new_src.name}"
            )
            return {
                "success": True,
                "message": f"Updated attachment to: {new_src.name}",
                "error":   "",
            }

        else:
            return {
                "success": False,
                "error": f"Unknown action: '{action}'. Valid values: read, delete, update.",
            }

    except Exception as exc:
        logger.error(f"[manage_received_attachment] Error: {exc}")
        return {"success": False, "error": str(exc)}


# ═════════════════════════════════════════════════════════════
# TOOLS REGISTRY  (exported list for agent registration)
# ═════════════════════════════════════════════════════════════

EMAIL_TOOLS = [
    send_email,
    read_emails,
    get_thread,
    reply_to_email,
    download_attachments,
    attach_files,
    list_contacts,
    list_sent_attachments,
    manage_sent_attachment,
    save_received_attachments,
    list_received_attachments,
    manage_received_attachment,
]
