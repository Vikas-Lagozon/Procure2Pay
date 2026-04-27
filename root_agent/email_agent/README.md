# Jarvis — Natural Language Email Agent

> **Lagozon Technology Pvt. Ltd.**
> An enterprise-grade, AI-powered email automation system built on the Google Agent Development Kit (ADK) and Gmail API.

---

## Table of Contents

- [Overview](#overview)
- [Architecture](#architecture)
- [Project Structure](#project-structure)
- [Prerequisites](#prerequisites)
- [Installation](#installation)
- [Gmail OAuth2 Setup](#gmail-oauth2-setup)
- [Environment Variables](#environment-variables)
- [Running the Agent](#running-the-agent)
- [Natural Language Usage](#natural-language-usage)
- [Intent & Tool Reference](#intent--tool-reference)
- [MongoDB Schema](#mongodb-schema)
- [Attachment Handling](#attachment-handling)
- [Thread Management](#thread-management)
- [Knowledge Base](#knowledge-base)
- [Security](#security)
- [Error Handling & Retries](#error-handling--retries)
- [Logging](#logging)
- [Production Deployment](#production-deployment)

---

## Overview

Jarvis is a conversational email agent that accepts **plain English instructions** and translates them into precise Gmail API calls — no manual configuration, no form-filling. It handles the full email lifecycle: composing, sending, replying, reading, thread tracking, and attachment management, all through a natural-language interface.

**Key capabilities:**

- Send new emails with or without attachments
- Reply to threads with correct RFC 2822 threading headers
- Read and search the inbox with Gmail query syntax
- Fetch and summarise full conversation threads
- Download attachments to structured local storage
- Auto-generate professional Lagozon-branded email bodies
- Resolve contact names to email addresses via MongoDB + Knowledge Base
- Persist all email events, threads, attachments, and contacts in MongoDB

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        run.py  (CLI Entry Point)                │
│              Interactive REPL  │  --once single-shot mode       │
└──────────────────────────┬──────────────────────────────────────┘
                           │ user input
                           ▼
┌─────────────────────────────────────────────────────────────────┐
│                       agent.py  (Orchestrator)                  │
│                                                                 │
│  ┌─────────────────┐    ┌──────────────────────────────────┐   │
│  │  Pre-processing │    │   ADK LlmAgent (Gemini)          │   │
│  │  ─────────────  │    │   ──────────────────────────     │   │
│  │  IntentParser   │───▶│   System Prompt + KB Context     │   │
│  │  EmailValidator │    │   Tool selection & invocation    │   │
│  └─────────────────┘    └──────────────┬───────────────────┘   │
│                                        │ tool calls             │
└────────────────────────────────────────┼────────────────────────┘
                                         │
                           ┌─────────────▼──────────────┐
                           │       tools.py              │
                           │                             │
                           │  send_email()               │
                           │  read_emails()              │
                           │  get_thread()               │
                           │  reply_to_email()           │
                           │  download_attachments()     │
                           │  attach_files()             │
                           └──────┬──────────┬───────────┘
                                  │          │
                     ┌────────────▼──┐  ┌────▼──────────────┐
                     │  Gmail API    │  │   MongoDB          │
                     │  (OAuth2)     │  │   ─────────────    │
                     │               │  │   emails           │
                     │  send         │  │   threads          │
                     │  read         │  │   attachments      │
                     │  threads      │  │   contacts         │
                     │  attachments  │  └────────────────────┘
                     └───────────────┘

┌─────────────────────────────────────────────────────────────────┐
│                      subagent.py  (Support Layer)               │
│                                                                 │
│  IntentParser      — NL → structured JSON intent               │
│  EmailValidator    — field validation before any API call       │
│  EmailBodyGenerator— Gemini-generated professional email bodies │
│  ThreadManager     — MongoDB thread lookup & tracking           │
│  ContactManager    — name → email resolution via MongoDB        │
│  KnowledgeBaseLoader— loads knowledge_base/ → injects context  │
└─────────────────────────────────────────────────────────────────┘
```

### Email Send Flow

```
User Input
    │
    ▼
IntentParser  ──── Gemini call ────▶  Structured JSON Intent
    │
    ▼
EmailValidator ────────────────────▶  Validation errors (if any, surfaced immediately)
    │
    ▼
LlmAgent decides tool + parameters
    │
    ▼
tools.send_email()
    │
    ├── _build_mime_message()   ← MIME + attachments
    ├── _encode_message()       ← base64url encode
    ├── Gmail API .send()       ← with _retry() (exponential back-off)
    ├── _store_email_record()   ← MongoDB emails collection
    └── _upsert_thread()        ← MongoDB threads collection
```

### Reply Flow

```
User: "Reply to thread abc123 saying we confirm by Friday"
    │
    ▼
get_thread(abc123)          ← fetch subject, last Message-ID, participants
    │
    ▼
Build MIME with headers:
    Subject    : Re: <original subject>
    In-Reply-To: <last message's Message-ID>
    References : <all prior Message-IDs>
    threadId   : abc123     ← attached to existing Gmail thread
    │
    ▼
Gmail API .send()  →  stored in MongoDB with status="replied"
```

---

## Project Structure

```
email_agent/
│
├── run.py                  # CLI entry point (interactive + single-shot)
├── agent.py                # ADK LlmAgent orchestrator + streaming chat
├── subagent.py             # Intent parsing, validation, thread & contact management
├── tools.py                # 6 Gmail API tools registered with the agent
├── prompts.py              # All LLM prompt templates
├── config.py               # Environment variable loading & validation
├── logger.py               # Centralised rotating-file logger
├── nosql_db.py             # MongoDB CRUD layer (MongoCollection)
│
├── knowledge_base/         # Drop .txt / .md / .json files here
│   └── Knowledge_Base.md   # Lagozon org info, contacts, templates
│
├── attachments/            # Auto-created; structured by thread_id
│   └── {thread_id}/
│       └── {timestamp}_{filename}
│
├── logs/                   # Auto-created rotating log files
│   └── log_YYYYMMDD_HHMMSS.log
│
├── credentials.json        # Gmail OAuth2 client credentials (you provide)
├── token.json              # Auto-generated on first OAuth flow
├── .env                    # Environment variables (never commit)
└── requirements.txt        # Python dependencies
```

---

## Prerequisites

| Requirement | Version |
|---|---|
| Python | 3.10+ |
| MongoDB | 6.0+ (local or Atlas) |
| Google Cloud Project | with Gmail API enabled |
| Gemini API key | from Google AI Studio |

---

## Installation

**1. Clone and create a virtual environment**

```bash
git clone <your-repo-url>
cd email_agent
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
```

**2. Install dependencies**

```bash
pip install -r requirements.txt
```

`requirements.txt`:

```
google-adk
google-generativeai
google-auth
google-auth-oauthlib
google-auth-httplib2
google-api-python-client
pymongo
python-dotenv
certifi
```

**3. Start MongoDB**

```bash
# Local
mongod --dbpath /data/db

# Or use MongoDB Atlas — paste the connection URI into .env
```

---

## Gmail OAuth2 Setup

Jarvis uses Gmail API with **OAuth2** — no passwords are stored.

**Step 1 — Enable Gmail API**

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create or select a project
3. Navigate to **APIs & Services → Library**
4. Search for **Gmail API** and click **Enable**

**Step 2 — Create OAuth2 Credentials**

1. Go to **APIs & Services → Credentials**
2. Click **Create Credentials → OAuth client ID**
3. Application type: **Desktop app**
4. Download the JSON file
5. Rename it to `credentials.json` and place it in the project root

**Step 3 — First Run (Browser Consent)**

```bash
python run.py
```

A browser window opens automatically. Sign in with the Gmail account Jarvis should control. Grant the requested permissions. The token is saved to `token.json` — subsequent runs are fully automatic (token auto-refreshes).

**Scopes requested:**

```
https://www.googleapis.com/auth/gmail.send
https://www.googleapis.com/auth/gmail.readonly
https://www.googleapis.com/auth/gmail.modify
```

---

## Environment Variables

Create a `.env` file in the project root:

```dotenv
# ── Gemini ────────────────────────────────────────────────────
GOOGLE_API_KEY=your_gemini_api_key_here
MODEL=gemini-2.5-flash

# ── Google Cloud ──────────────────────────────────────────────
GOOGLE_APPLICATION_CREDENTIALS=path/to/service_account.json
BUCKET_NAME=your_gcs_bucket_name

# ── Gmail OAuth2 ──────────────────────────────────────────────
GMAIL_CREDENTIALS_FILE=credentials.json
GMAIL_TOKEN_FILE=token.json

# ── Email Defaults ────────────────────────────────────────────
DEFAULT_SENDER_EMAIL=vikas.prajapati@lagozon.com
DEFAULT_SENDER_NAME=Vikas Prajapati

# ── Attachments ───────────────────────────────────────────────
ATTACHMENT_DIR=attachments
MAX_ATTACHMENT_SIZE_MB=25

# ── MongoDB ───────────────────────────────────────────────────
MONGO_URI=mongodb://localhost:27017
MONGO_DB=jarvis_email_db
# MONGO_USER=your_user        # uncomment if auth is enabled
# MONGO_PASSWORD=your_pass

# ── Retry Policy ─────────────────────────────────────────────
EMAIL_MAX_RETRIES=3
EMAIL_RETRY_DELAY=2.0

# ── Knowledge Base ────────────────────────────────────────────
KNOWLEDGE_BASE_DIR=knowledge_base

# ── Misc ─────────────────────────────────────────────────────
EMAIL_FETCH_MAX_RESULTS=10
DEBUG=false
```

> **Never commit `.env`, `credentials.json`, or `token.json` to version control.**
> Add them all to `.gitignore`.

---

## Running the Agent

**Interactive mode (default)**

```bash
python run.py
```

**Resume a named session** (conversation history is preserved within the process)

```bash
python run.py --session my_session_id
```

**Single-shot mode** (for scripting or CI)

```bash
python run.py --once "Show me my unread emails from today"
```

**In-process CLI commands**

| Command | Action |
|---|---|
| `exit` / `quit` | End the session |
| `session` | Print the current session ID |
| `clear` | Clear the terminal |
| `help` | Show usage help |

---

## Natural Language Usage

Jarvis understands plain English. Here are real example prompts and what they trigger:

| User Input | Action Invoked |
|---|---|
| `Show me my unread emails` | `read_emails(query="is:unread")` |
| `Show emails from john@acme.com this week` | `read_emails(query="from:john@acme.com newer_than:7d")` |
| `Send a proposal to alice@corp.com` | `send_email(to=["alice@corp.com"], ...)` |
| `Email alice@corp.com and cc bob@corp.com the Q3 report` | `send_email(to=[...], cc=[...], ...)` |
| `Reply to thread abc123 saying we'll deliver by Friday` | `reply_to_email(thread_id="abc123", ...)` |
| `Get the full conversation for thread abc123` | `get_thread(thread_id="abc123")` |
| `Download all invoice attachments from last week` | `download_attachments(query="subject:invoice newer_than:7d")` |
| `Attach /reports/Q3.pdf before sending` | `attach_files(file_paths=["/reports/Q3.pdf"])` |

**Auto-body generation:** If you don't specify email content, Jarvis generates a professional, Lagozon-branded email body based on your intent and the recipient context.

---

## Intent & Tool Reference

### Structured Intent Format

Every natural-language instruction is first parsed into this JSON structure internally:

```json
{
  "action": "send_email",
  "to": ["alice@corp.com"],
  "cc": ["bob@corp.com"],
  "bcc": [],
  "subject": "Q3 Proposal — Lagozon Technology",
  "body": "Dear Alice, ...",
  "attachments": ["/reports/Q3.pdf"],
  "thread_id": "",
  "message_id": "",
  "query": "",
  "max_results": 10,
  "download_dir": ""
}
```

### Tool Signatures

```python
send_email(to, subject, body, cc=[], bcc=[], attachments=[], sender=None)
    → {success, message_id, thread_id, error}

read_emails(query="is:unread", max_results=10)
    → {success, emails: [{message_id, thread_id, subject, sender, date, snippet}], error}

get_thread(thread_id)
    → {success, thread_id, subject, messages: [{...full headers + body}], error}

reply_to_email(thread_id, body, message_id="", to=[], cc=[], attachments=[], sender=None)
    → {success, message_id, thread_id, error}

download_attachments(query="", thread_id="", message_id="", download_dir="")
    → {success, downloaded: [file_paths], count, error}

attach_files(file_paths)
    → {success, valid_paths, invalid: [{path, reason}], error}
```

---

## MongoDB Schema

### `emails` collection

```json
{
  "_id": "ObjectId",
  "message_id": "Gmail message ID",
  "thread_id": "Gmail thread ID",
  "sender": "vikas.prajapati@lagozon.com",
  "to": ["alice@corp.com"],
  "cc": ["bob@corp.com"],
  "bcc": [],
  "subject": "Q3 Proposal",
  "body": "Full email body text",
  "attachments": ["/path/to/file.pdf"],
  "in_reply_to": "RFC 2822 Message-ID of parent",
  "gmail_message_id": "<unique-rfc2822-id@gmail.com>",
  "status": "sent | failed | read | replied",
  "timestamp": "2025-07-15T10:30:00Z",
  "error": ""
}
```

### `threads` collection

```json
{
  "_id": "ObjectId",
  "thread_id": "Gmail thread ID",
  "subject": "Q3 Proposal",
  "participants": ["vikas@lagozon.com", "alice@corp.com"],
  "created_at": "2025-07-15T10:30:00Z",
  "last_updated": "2025-07-15T14:22:00Z"
}
```

### `attachments` collection

```json
{
  "_id": "ObjectId",
  "message_id": "Gmail message ID",
  "thread_id": "Gmail thread ID",
  "filename": "invoice_july.pdf",
  "size_bytes": 204800,
  "local_path": "attachments/abc123/20250715_103000_invoice_july.pdf",
  "downloaded_at": "2025-07-15T10:30:00Z"
}
```

### `contacts` collection

```json
{
  "_id": "ObjectId",
  "email": "alice@corp.com",
  "name": "Alice Johnson",
  "organisation": "Corp Ltd.",
  "created_at": "2025-07-15T10:30:00Z",
  "updated_at": "2025-07-15T14:22:00Z"
}
```

**Recommended indexes:**

```js
db.emails.createIndex({ thread_id: 1 })
db.emails.createIndex({ status: 1, timestamp: -1 })
db.threads.createIndex({ thread_id: 1 }, { unique: true })
db.threads.createIndex({ subject: "text" })
db.contacts.createIndex({ email: 1 }, { unique: true })
db.contacts.createIndex({ name: "text" })
```

---

## Attachment Handling

Downloaded and outbound attachments are managed through a structured directory layout:

```
attachments/
└── {gmail_thread_id}/
    ├── 20250715_103000_invoice_july.pdf
    ├── 20250715_103005_purchase_order.xlsx
    └── 20250715_140000_signed_contract.pdf
```

**Validation rules (enforced before any send):**

- File must exist on disk
- File must be a regular file (not a directory or symlink)
- File size must not exceed `MAX_ATTACHMENT_SIZE_MB` (default: 25 MB)
- File path must resolve within the working directory or the `attachments/` folder — path traversal attacks are rejected

**Metadata** for every downloaded attachment is stored in the `attachments` MongoDB collection including filename, size, thread ID, and local path.

---

## Thread Management

Jarvis maintains full RFC 2822 threading compliance:

| Header | Purpose |
|---|---|
| `Message-ID` | Unique identifier for each sent message |
| `In-Reply-To` | References the immediate parent message |
| `References` | Full chain of all prior Message-IDs in the thread |

When replying, Jarvis automatically:
1. Calls `get_thread()` to retrieve the last message's `Message-ID`
2. Sets `In-Reply-To` to that ID
3. Builds the full `References` chain
4. Prepends `Re:` to the subject (if not already present)
5. Attaches the reply to the existing Gmail `threadId`

The result is that replies appear correctly grouped in all email clients (Gmail, Outlook, Apple Mail).

Thread metadata is also stored in MongoDB (`threads` collection) so subsequent lookups — like "find the invoice thread" — can be resolved without a Gmail API call.

---

## Knowledge Base

Place any `.txt`, `.md`, or `.json` files into the `knowledge_base/` directory. They are loaded at startup and injected into the agent's system prompt.

The included `Knowledge_Base.md` provides Jarvis with:

- Full Lagozon company profile, locations, and contact info
- Services, AI products, technology partners, and industries served
- Vikas Prajapati's operator identity, email signature, and communication guidelines
- Standard email and WhatsApp templates
- Key client and partner lists

The `KnowledgeBaseLoader` also exposes `find_contact_in_kb()` to resolve names mentioned in instructions (e.g. "Send to the EY team") into email addresses using regex search over the KB text, falling back to MongoDB contacts.

---

## Security

| Concern | Implementation |
|---|---|
| OAuth2 tokens | Stored in `token.json` (local file, never in DB or logs) |
| API keys | Loaded exclusively from `.env` via `python-dotenv` |
| Path traversal | `_safe_attachment_path()` resolves and validates all paths against permitted dirs |
| File size limits | Enforced before reading or sending any attachment |
| Sensitive logging | Tokens, passwords, and raw email bodies are never logged at INFO level |
| MongoDB credentials | Built into `MONGO_URI` from env vars; never hardcoded |
| `.gitignore` | `.env`, `credentials.json`, `token.json`, `logs/`, `attachments/` should all be excluded |

**Recommended `.gitignore` additions:**

```
.env
credentials.json
token.json
token.pickle
logs/
attachments/
*.pyc
__pycache__/
.venv/
```

---

## Error Handling & Retries

**Automatic retry with exponential back-off:**

```
Attempt 1  →  fails  →  wait 2s
Attempt 2  →  fails  →  wait 4s
Attempt 3  →  fails  →  raise exception
```

Configurable via `EMAIL_MAX_RETRIES` and `EMAIL_RETRY_DELAY` in `.env`.

**Error categories handled:**

| Error | Behaviour |
|---|---|
| Invalid email address | Caught pre-API call; returned as user-facing message |
| Missing attachment file | Caught in `attach_files()` before MIME assembly |
| File exceeds size limit | Caught per-file; remaining files still processed |
| Gmail API `HttpError` | Retried up to `EMAIL_MAX_RETRIES` times; failure stored in MongoDB with `status="failed"` |
| OAuth token expired | Auto-refreshed transparently by `GmailAuthManager` |
| MongoDB write failure | Logged as warning; email operation continues (email delivery is not blocked by DB failures) |
| Unknown intent | Passed to the ADK agent for clarification; never silently dropped |

---

## Logging

All activity is written to both the console (INFO+) and a rotating log file (DEBUG+):

```
logs/log_20250715_103000.log
```

**Log format:**

```
2025-07-15 10:30:00 | INFO     | Jarvis.tools | [send_email] Sent ✓ | message_id=18a... thread_id=18a...
2025-07-15 10:30:01 | DEBUG    | Jarvis.nosql_db | [emails] insert_one → _id=6...
2025-07-15 10:30:05 | WARNING  | Jarvis.tools | [Retry] Attempt 1/3 failed: HttpError 429
```

Log files are named with the process start timestamp so multiple runs never overwrite each other. Use `get_logger(__name__)` in any new module to inherit the shared handlers automatically.

---

## Production Deployment

**Containerisation (Docker)**

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["python", "run.py"]
```

Mount `.env`, `credentials.json`, and `token.json` as Docker secrets or volume mounts — never bake them into the image.

**Session persistence**

The default `InMemorySessionService` in `agent.py` does not survive process restarts. For production, swap it for the ADK `DatabaseSessionService` backed by PostgreSQL or Redis:

```python
from google.adk.sessions import DatabaseSessionService
session_service = DatabaseSessionService(db_url=config.HITL_DB_URL)
```

**Scaling**

For high-volume workloads, decouple email execution from the agent loop using a task queue:

```
User Request → Agent (intent parse) → Celery Task Queue → Worker (Gmail API)
                                                        ↓
                                               MongoDB (result store)
```

Recommended stack: **Celery + Redis** for the queue, **Flower** for monitoring.

**Rate limiting**

Gmail API default quota is 250 quota units/second per user. For bulk sends, implement a token-bucket rate limiter in `tools.py` or use `time.sleep()` between batched calls. Monitor usage in [Google Cloud Console → APIs & Services → Quotas](https://console.cloud.google.com/).

**Health check endpoint**

For service deployments, wrap `agent.py` in a FastAPI app and expose:

```
GET /health  →  {"status": "ok", "mongo": "connected", "gmail": "authenticated"}
```

---

## Module Summary

| File | Lines | Responsibility |
|---|---|---|
| `tools.py` | ~1,013 | Gmail API tools, OAuth2, MIME, retry logic, MongoDB writes |
| `subagent.py` | ~592 | Intent parsing, validation, body generation, thread & contact management |
| `prompts.py` | ~262 | All LLM prompt templates (system, intent extraction, body generation) |
| `run.py` | ~221 | CLI entry point, interactive loop, single-shot mode |
| `agent.py` | ~235 | ADK orchestrator, session management, streaming chat |
| `config.py` | ~96 | Environment loading and validation |

---

## Licence

Internal use only — Lagozon Technology Pvt. Ltd. © 2025. All rights reserved.
