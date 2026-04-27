# Jarvis — Natural Language Email Agent

> **Lagozon Technology Pvt. Ltd.**
> An enterprise-grade, AI-powered email automation system built on the
> Google Agent Development Kit (ADK) and Gmail API.

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

Jarvis is a conversational email agent that accepts **plain English instructions**
and translates them into precise Gmail API calls — no manual configuration,
no form-filling. It handles the full email lifecycle: composing, sending,
replying, reading, thread tracking, and attachment management, all through
a natural-language interface powered by Gemini.

**Key capabilities:**

- Send new emails with or without attachments
- Reply to threads with correct RFC 2822 threading headers (`In-Reply-To`, `References`)
- Read and search the inbox using full Gmail query syntax
- Fetch and summarise full conversation threads
- Download attachments to structured local storage
- Auto-generate professional Lagozon-branded email bodies when no body is provided
- Resolve contact names to email addresses via MongoDB + Knowledge Base
- Persist all email events, threads, attachments, and contacts in MongoDB
- Pre-validate every request before touching the Gmail API (invalid addresses caught immediately)

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                     run.py  (CLI Entry Point)                   │
│          Interactive REPL  │  --once  │  --session              │
└──────────────────────────┬──────────────────────────────────────┘
                           │ user input
                           ▼
┌─────────────────────────────────────────────────────────────────┐
│                    agent.py  (Orchestrator)                     │
│                                                                 │
│  ┌───────────────────┐    ┌────────────────────────────────┐   │
│  │  Pre-processing   │    │  ADK LlmAgent  (Gemini)        │   │
│  │  ─────────────── │    │  ──────────────────────────    │   │
│  │  IntentParser     │───▶│  System Prompt + KB Context    │   │
│  │  EmailValidator   │    │  Tool selection & invocation   │   │
│  └───────────────────┘    └──────────────┬─────────────────┘   │
│                                          │ tool calls           │
└──────────────────────────────────────────┼──────────────────────┘
                                           │
                             ┌─────────────▼──────────────┐
                             │         tools.py            │
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
│                   subagent.py  (Support Layer)                  │
│                                                                 │
│  IntentParser       — Natural language → structured JSON intent │
│  EmailValidator     — Field validation before any API call      │
│  EmailBodyGenerator — Gemini-generated professional email bodies│
│  ThreadManager      — MongoDB thread lookup & RFC 2822 tracking │
│  ContactManager     — Name → email resolution via MongoDB       │
│  KnowledgeBaseLoader— Loads knowledge_base/ → injects context  │
│  EmailWorkflow      — Chains all of the above end-to-end       │
└─────────────────────────────────────────────────────────────────┘
```

### Email Send Flow

```
User Input (plain English)
    │
    ▼
IntentParser  ──── Gemini call ────▶  Structured JSON Intent
    │
    ▼
EmailValidator ────────────────────▶  Errors surfaced immediately (no API call wasted)
    │
    ▼
ADK LlmAgent selects tool + parameters
    │
    ▼
tools.send_email()
    │
    ├── _build_mime_message()    ← MIME assembly + base64 attachments
    ├── _encode_message()        ← base64url encode for Gmail API
    ├── Gmail API .send()        ← with _retry() exponential back-off
    ├── _store_email_record()    ← MongoDB  emails  collection
    └── _upsert_thread()         ← MongoDB  threads  collection
```

### Reply-in-Thread Flow

```
User: "Reply to thread abc123 — confirm delivery by Friday"
    │
    ▼
get_thread("abc123")         ← fetch subject, participants, last Message-ID
    │
    ▼
Build MIME with RFC 2822 headers:
    Subject    : Re: <original subject>
    In-Reply-To: <last message's Message-ID>
    References : <full chain of all prior Message-IDs>
    threadId   : abc123  ← attaches reply to the existing Gmail thread
    │
    ▼
Gmail API .send()  →  stored in MongoDB as  status="replied"
```

---

## Project Structure

```
email_agent/
│
├── run.py                   # CLI entry point — interactive + single-shot modes
├── agent.py                 # ADK LlmAgent orchestrator + streaming chat interface
├── subagent.py              # Intent parsing, validation, thread & contact management
├── tools.py                 # 6 Gmail API tools registered with the ADK agent
├── prompts.py               # All LLM prompt templates (system, intent, body gen)
├── config.py                # .env loading, validation, and typed config object
├── logger.py                # Centralised rotating-file logger (shared handlers)
├── nosql_db.py              # MongoDB CRUD layer (MongoCollection wrapper)
│
├── knowledge_base/          # Drop .txt / .md / .json files here — loaded at startup
│   └── Knowledge_Base.md    # Lagozon org info, contacts, templates, comm guidelines
│
├── attachments/             # Auto-created; structured by Gmail thread_id
│   └── {thread_id}/
│       └── {YYYYMMDD_HHMMSS}_{filename}
│
├── logs/                    # Auto-created; one timestamped file per process start
│   └── log_YYYYMMDD_HHMMSS.log
│
├── credentials.json         # Gmail OAuth2 client secret — YOU provide this
├── token.json               # Auto-generated after first browser consent flow
├── .env                     # All secrets and configuration  ← NEVER COMMIT
├── .env.example             # Safe template to commit — placeholder values only
└── requirements.txt         # Python dependencies
```

---

## Prerequisites

| Requirement | Version / Notes |
|---|---|
| Python | 3.10 or higher |
| MongoDB | 6.0+ — local instance or MongoDB Atlas |
| Google Cloud Project | Gmail API enabled (see setup below) |
| Gemini API Key | From [Google AI Studio](https://aistudio.google.com/) |
| `credentials.json` | OAuth2 Desktop App credentials from GCP Console |

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

# MongoDB Atlas — paste your connection string into .env as MONGO_URI
```

**4. Set up your environment file**

```bash
cp .env.example .env
# Open .env and fill in your real values
```

---

## Gmail OAuth2 Setup

Jarvis uses Gmail API with **OAuth2** — no passwords are ever stored.

### Step 1 — Enable the Gmail API

1. Open [Google Cloud Console](https://console.cloud.google.com/)
2. Select or create a project
3. Navigate to **APIs & Services → Library**
4. Search **Gmail API** → click **Enable**

### Step 2 — Create OAuth2 Credentials

1. Go to **APIs & Services → Credentials**
2. Click **Create Credentials → OAuth client ID**
3. Application type: **Desktop app**
4. Click **Download JSON**
5. Rename the file to `credentials.json` and place it in the project root

### Step 3 — First-Run Browser Consent

```bash
python run.py
```

A browser window opens automatically. Sign in with the Gmail account Jarvis
will control, then grant the requested permissions. The access token is saved
to `token.json` — all future runs are fully automatic (token auto-refreshes).

### OAuth2 Scopes Requested

| Scope | Purpose |
|---|---|
| `gmail.send` | Send new emails and replies |
| `gmail.readonly` | Read inbox, search messages, fetch threads |
| `gmail.modify` | Mark messages as read, modify labels |
| `contacts.readonly` | Resolve contact names to email addresses |

> **Important:** If you previously ran the agent without the contacts scope,
> delete `token.json` and re-run to trigger a fresh consent flow that includes
> all four scopes above.

---

## Environment Variables

Copy `.env.example` to `.env` and fill in your actual values. The table below
describes every key.

### Full `.env` Template

```dotenv
# ── Model ─────────────────────────────────────────────────────
MODEL="gemini-2.5-flash"

# ── GCP Global ────────────────────────────────────────────────
GOOGLE_GENAI_USE_VERTEXAI=0
GOOGLE_CLOUD_LOCATION=us-central1
GOOGLE_CLOUD_PROJECT=GenAI
PROJECT_ID=your-gcp-project-id

# ── Gemini (API Key Mode) ─────────────────────────────────────
GOOGLE_API_KEY=your_gemini_api_key
GOOGLE_CSE_ID=your_custom_search_engine_id
GOOGLE_MAPS_API_KEY=your_maps_api_key

# ── GCS Artifact Service ──────────────────────────────────────
GOOGLE_APPLICATION_CREDENTIALS=your_service_account.json
BUCKET_NAME=your_gcs_bucket_name

# ── Gmail OAuth2 ──────────────────────────────────────────────
GMAIL_CREDENTIALS_FILE=credentials.json
GMAIL_TOKEN_FILE=token.json

# ── Email Defaults ────────────────────────────────────────────
DEFAULT_SENDER_EMAIL=vikas.prajapati@lagozon.com
DEFAULT_SENDER_NAME=Vikas Prajapati

# ── Attachment Storage ────────────────────────────────────────
ATTACHMENT_DIR=attachments
MAX_ATTACHMENT_SIZE_MB=25

# ── Email Retry Policy ────────────────────────────────────────
EMAIL_MAX_RETRIES=3
EMAIL_RETRY_DELAY=2.0

# ── Email Fetch Defaults ──────────────────────────────────────
EMAIL_FETCH_MAX_RESULTS=10

# ── Knowledge Base ────────────────────────────────────────────
KNOWLEDGE_BASE_DIR=knowledge_base

# ── PostgreSQL ────────────────────────────────────────────────
PG_USER="procure2pay"
PG_PASSWORD="abcd1234"
PG_HOST="localhost"
PG_PORT=5432
PG_DB="procure2pay_db"

# ── MongoDB ───────────────────────────────────────────────────
MONGO_URI="mongodb://localhost:27017"
MONGO_DB="jarvis_email_db"
MONGO_USER=""
MONGO_PASSWORD=""

# ── Session Management ────────────────────────────────────────
DB_SCHEMA="public"

# ── Debug ─────────────────────────────────────────────────────
DEBUG=false
```

### Variable Reference

| Variable | Required | Default | Description |
|---|---|---|---|
| `GOOGLE_API_KEY` | ✅ | — | Gemini API key from Google AI Studio |
| `GOOGLE_APPLICATION_CREDENTIALS` | ✅ | — | Path to GCP service account JSON |
| `BUCKET_NAME` | ✅ | — | GCS bucket for artifact storage |
| `GMAIL_CREDENTIALS_FILE` | ✅ | `credentials.json` | OAuth2 Desktop App credentials file |
| `GMAIL_TOKEN_FILE` | — | `token.json` | OAuth token file (auto-generated, do not edit) |
| `DEFAULT_SENDER_EMAIL` | — | `vikas.prajapati@lagozon.com` | Default From address for all outbound email |
| `DEFAULT_SENDER_NAME` | — | `Vikas Prajapati` | Display name for the sender |
| `MODEL` | — | `gemini-2.5-flash` | Gemini model identifier |
| `MONGO_URI` | — | `mongodb://localhost:27017` | MongoDB connection string |
| `MONGO_DB` | — | `jarvis_email_db` | MongoDB database name |
| `MONGO_USER` | — | `` | MongoDB username (leave blank if auth disabled) |
| `MONGO_PASSWORD` | — | `` | MongoDB password (leave blank if auth disabled) |
| `ATTACHMENT_DIR` | — | `attachments` | Root directory for downloaded attachments |
| `MAX_ATTACHMENT_SIZE_MB` | — | `25` | Per-file size cap in megabytes |
| `EMAIL_MAX_RETRIES` | — | `3` | Gmail API retry attempts before failing |
| `EMAIL_RETRY_DELAY` | — | `2.0` | Base delay (seconds) between retries |
| `EMAIL_FETCH_MAX_RESULTS` | — | `10` | Default emails fetched per `read_emails()` call |
| `KNOWLEDGE_BASE_DIR` | — | `knowledge_base` | Path to the Knowledge Base directory |
| `GOOGLE_GENAI_USE_VERTEXAI` | — | `0` | Set to `0` for API key mode, `1` for Vertex AI |
| `DEBUG` | — | `false` | Set to `true` for verbose DEBUG console output |

---

## Running the Agent

### Interactive mode (default)

```bash
python run.py
```

### Resume a named session

Conversation history is preserved in memory for the same session ID
within a single process.

```bash
python run.py --session my_session_id
```

### Single-shot mode (scripting / CI)

```bash
python run.py --once "Show me my unread emails from today"
```

### Built-in CLI commands

| Command | Action |
|---|---|
| `exit` / `quit` / `bye` | End the session gracefully |
| `session` | Print the current session ID |
| `clear` | Clear the terminal (in-memory history preserved) |
| `help` | Show the usage banner |

---

## Natural Language Usage

Jarvis understands plain English. Here are example prompts and the tools they invoke:

| User Input | Tool Invoked |
|---|---|
| `Show me my unread emails` | `read_emails(query="is:unread")` |
| `Emails from john@acme.com this week` | `read_emails(query="from:john@acme.com newer_than:7d")` |
| `Show all emails with invoices attached` | `read_emails(query="subject:invoice has:attachment")` |
| `Send a proposal to alice@corp.com` | `send_email(to=["alice@corp.com"], ...)` |
| `Email alice and cc bob@corp.com the Q3 report` | `send_email(to=[...], cc=[...], ...)` |
| `Send the intro email to EY` | `send_email(...)` ← body auto-generated from KB |
| `Reply to thread abc123 — delivery confirmed Friday` | `reply_to_email(thread_id="abc123", ...)` |
| `Get the full conversation for thread abc123` | `get_thread(thread_id="abc123")` |
| `Download all invoice attachments from last week` | `download_attachments(query="subject:invoice newer_than:7d")` |
| `Validate /reports/Q3.pdf before I send it` | `attach_files(file_paths=["/reports/Q3.pdf"])` |

**Auto-body generation:** If your instruction doesn't include email content,
`EmailBodyGenerator` produces a professional, Lagozon-branded body based on
the recipient, subject, and your stated intent — then appends the standard
signature automatically.

---

## Intent & Tool Reference

### Structured Intent Format

Every natural-language instruction is first parsed into this internal JSON
by `IntentParser` before any tool is called:

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

Valid `action` values: `send_email` · `reply_to_email` · `read_emails` ·
`get_thread` · `download_attachments` · `attach_files`

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
  "message_id": "Gmail internal message ID",
  "thread_id": "Gmail thread ID",
  "gmail_message_id": "<rfc2822-unique-id@gmail.com>",
  "sender": "vikas.prajapati@lagozon.com",
  "to": ["alice@corp.com"],
  "cc": ["bob@corp.com"],
  "bcc": [],
  "subject": "Q3 Proposal",
  "body": "Full email body text",
  "attachments": ["/path/to/file.pdf"],
  "in_reply_to": "RFC 2822 Message-ID of the parent message",
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

### Recommended Indexes

```js
db.emails.createIndex({ thread_id: 1 })
db.emails.createIndex({ status: 1, timestamp: -1 })
db.threads.createIndex({ thread_id: 1 }, { unique: true })
db.threads.createIndex({ subject: "text" })
db.contacts.createIndex({ email: 1 }, { unique: true })
db.contacts.createIndex({ name: "text" })
db.attachments.createIndex({ thread_id: 1 })
```

---

## Attachment Handling

All downloaded and outbound attachments follow a structured directory layout:

```
attachments/
└── {gmail_thread_id}/
    ├── 20250715_103000_invoice_july.pdf
    ├── 20250715_103005_purchase_order.xlsx
    └── 20250715_140000_signed_contract.pdf
```

**Validation rules enforced before any send or download:**

- File must exist on disk and be a regular file (not a symlink or directory)
- File size must not exceed `MAX_ATTACHMENT_SIZE_MB` (default: 25 MB)
- Resolved path must remain within the CWD or `attachments/` base directory —
  path traversal attempts are rejected by `_safe_attachment_path()`

All downloaded attachment metadata (filename, size, thread ID, local path)
is persisted to the `attachments` MongoDB collection for auditability.

---

## Thread Management

Jarvis maintains full **RFC 2822 threading compliance** so replies appear
correctly grouped in all email clients (Gmail, Outlook, Apple Mail):

| Header | Purpose |
|---|---|
| `Message-ID` | Unique identifier auto-assigned by Gmail to each sent message |
| `In-Reply-To` | Set to the `Message-ID` of the message being replied to |
| `References` | Full ordered chain of all prior `Message-ID`s in the thread |

When replying, Jarvis automatically:

1. Calls `get_thread()` to retrieve the full message list in the thread
2. Reads the last message's `Message-ID` from the RFC 2822 headers
3. Sets `In-Reply-To` and assembles the full `References` chain
4. Prepends `Re:` to the subject if not already present
5. Sets `threadId` in the Gmail API request body to attach to the existing thread

Thread metadata is also persisted in MongoDB (`threads` collection), enabling
fast lookups like "find the invoice thread" without an additional Gmail API call.

---

## Knowledge Base

Place `.txt`, `.md`, or `.json` files into the `knowledge_base/` directory.
They are loaded once at startup and injected directly into the ADK agent's
system prompt via `KnowledgeBaseLoader`.

The included `Knowledge_Base.md` gives Jarvis full context about:

- Lagozon company profile, office locations, and contact details
- Services and AI products: InsightAgent AI, IntelliDoc AI, DBQuery AI, RetailPulse AI
- Technology partners: Microsoft, Google Cloud, Databricks, Snowflake, AWS, Qlik
- Industries served and key clients (EY, Indian Oil, GeM, Virtusa, Protiviti…)
- Vikas Prajapati's operator identity, email signature, and communication guidelines
- Standard email and WhatsApp message templates ready for reuse

`KnowledgeBaseLoader.find_contact_in_kb()` uses regex search across all loaded
KB text to resolve names like "EY" or "Indian Oil" to email addresses, with
`ContactManager` (MongoDB) as a secondary fallback.

---

## Security

| Concern | Implementation |
|---|---|
| OAuth2 tokens | Stored only in `token.json` on disk — never logged or persisted to DB |
| API keys & passwords | Loaded exclusively from `.env` via `python-dotenv` — never hardcoded |
| Path traversal | `_safe_attachment_path()` resolves and validates all paths against permitted dirs |
| File size enforcement | Checked before reading or sending — no silent oversized transfers |
| Log sanitisation | Tokens, passwords, and raw bodies never appear at INFO level |
| MongoDB credentials | Constructed from env vars into `MONGO_URI` at runtime |

**Required `.gitignore` entries — add these immediately:**

```gitignore
.env
credentials.json
token.json
token.pickle
logs/
attachments/
*.pyc
__pycache__/
.venv/
*.json
!.env.example
```

---

## Error Handling & Retries

**Automatic retry with exponential back-off:**

```
Attempt 1  →  fails  →  wait 2 s
Attempt 2  →  fails  →  wait 4 s
Attempt 3  →  fails  →  raise → stored in MongoDB as  status="failed"
```

Configured via `EMAIL_MAX_RETRIES` and `EMAIL_RETRY_DELAY` in `.env`.

**Error categories and behaviour:**

| Error | Behaviour |
|---|---|
| Invalid email address | Caught pre-API by `EmailValidator`; user-facing message returned immediately |
| Missing `thread_id` for reply | Caught by validator; user prompted to fetch it first |
| Attachment file not found | Caught in `attach_files()`; remaining files still validated |
| File exceeds size limit | That file is skipped with a warning; rest of the operation continues |
| Gmail API `HttpError` (4xx/5xx) | Retried up to `EMAIL_MAX_RETRIES`; final failure logged to MongoDB |
| OAuth token expired | Auto-refreshed transparently by `GmailAuthManager` singleton |
| MongoDB write failure | Logged as `WARNING`; email delivery is never blocked by a DB error |
| Ambiguous user instruction | Passed to the ADK agent for clarification — never silently dropped |

---

## Logging

All activity is written to both the console (`INFO` and above) and a
timestamped log file (`DEBUG` and above):

```
logs/log_20250715_103000.log
```

**Log format:**

```
2025-07-15 10:30:00 | INFO     | Jarvis.tools    | [send_email] Sent ✓ | message_id=18a... thread_id=18a...
2025-07-15 10:30:01 | DEBUG    | Jarvis.nosql_db | [emails] insert_one → _id=6abc...
2025-07-15 10:30:05 | WARNING  | Jarvis.tools    | [Retry] Attempt 1/3 failed: HttpError 429
2025-07-15 10:30:07 | ERROR    | Jarvis.agent    | [Chat] Agent error: <exception detail>
```

Each process start creates a new, uniquely named log file so no run ever
overwrites a previous one. Use `get_logger(__name__)` in any new module to
inherit the shared file and console handlers automatically.

---

## Production Deployment

### Docker

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["python", "run.py"]
```

Mount `.env`, `credentials.json`, and `token.json` as Docker secrets or
named volumes. Never bake secrets into the image layer.

### Session Persistence

The default `InMemorySessionService` in `agent.py` does not survive process
restarts. For persistent production sessions, swap it for the ADK
`DatabaseSessionService`:

```python
from google.adk.sessions import DatabaseSessionService
session_service = DatabaseSessionService(
    db_url="postgresql+asyncpg://user:pass@host/db"
)
```

### Task Queue for High-Volume Workflows

For bulk email operations, decouple intent parsing from Gmail execution
using a task queue:

```
User Request → Agent (intent + validation) → Celery Queue → Worker (Gmail API)
                                                          ↓
                                                 MongoDB (results + status)
```

Recommended stack: **Celery + Redis** for the queue, **Flower** for monitoring.

### Gmail API Rate Limits

The Gmail API default quota is **250 units per second per user**. For bulk
sends, implement a token-bucket rate limiter in `tools.py` or add
`time.sleep()` between batches. Monitor live usage at:
Google Cloud Console → APIs & Services → Quotas.

### Health Check Endpoint

Wrap the agent in **FastAPI** for service deployments and expose a health check:

```python
from fastapi import FastAPI
app = FastAPI()

@app.get("/health")
async def health():
    return {
        "status":  "ok",
        "mongo":   "connected",
        "gmail":   "authenticated",
        "model":   config.MODEL,
    }
```

---

## Module Summary

| File | Lines | Responsibility |
|---|---|---|
| `tools.py` | ~1,013 | Gmail API tools, `GmailAuthManager` OAuth2, MIME building, retry, MongoDB writes |
| `subagent.py` | ~592 | Intent parsing, validation, body generation, thread & contact management, KB loading |
| `prompts.py` | ~262 | All LLM prompts — system instruction, intent extraction, body generation |
| `run.py` | ~221 | CLI entry point — interactive REPL, `--once`, `--session`, ANSI colours |
| `agent.py` | ~235 | ADK orchestrator, KB injection into system prompt, pre-processing hook, streaming |
| `config.py` | ~96 | `.env` loading, type coercions, missing-key validation |
| `logger.py` | ~60 | Shared rotating-file logger — one bootstrap, shared handlers across all modules |
| `nosql_db.py` | — | `MongoCollection` CRUD wrapper used by tools, subagent, and agent |

---

## Licence

Internal use only — Lagozon Technology Pvt. Ltd. © 2025. All rights reserved.
