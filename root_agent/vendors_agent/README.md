# Vendors Agent

A **fully natural-language-driven** vendor management module built on top of Google ADK. Users interact in plain English to upload, list, view, update, and delete vendor documents. The agent understands intent automatically — no slash commands required.

---

## Table of Contents

- [Overview](#overview)
- [Directory Structure](#directory-structure)
- [Architecture](#architecture)
- [File Reference](#file-reference)
- [Setup](#setup)
- [Running the Agent](#running-the-agent)
- [Usage Guide](#usage-guide)
- [How Intent Detection Works](#how-intent-detection-works)
- [Data Flow](#data-flow)
- [Storage](#storage)
- [Environment Variables](#environment-variables)
- [Integration with Root Agent](#integration-with-root-agent)

---

## Overview

| Capability | Example user message |
|---|---|
| Upload a document | `"Upload docs/dell_india_vendor.docx"` |
| List all records | `"Show all vendors"` |
| View a record | `"Show details of the Dell vendor"` |
| Delete one record | `"Delete the Dell vendor"` |
| Delete all records | `"Remove all vendors"` |
| Delete multiple | `"Delete both of them"` |
| Update a record | `"Replace the printer supplier with new_spec.docx"` |
| Ask questions | `"Which vendor offers the lowest price for laptops?"` |
| Filter by category | `"List all vendors that supply electronics"` |
| Cross-doc analysis | `"Compare delivery lead times across all vendors"` |

---

## Directory Structure

```
vendors_agent/
├── VENDORS/                              # Physical document storage
│   ├── 20260421_043634_vendor_1.docx
│   ├── 20260421_043701_vendor_2.docx
│   └── ...
│
├── __init__.py
├── agent.py                              # Core agent + intent detection
├── tools.py                              # CRUD + Q&A handler functions
├── prompts.py                            # All LLM prompts (intent, structurer, qa, help)
├── utils.py                              # Text extraction, JSON helpers, formatters
├── config.py                             # Environment config (shared with root)
├── nosql_db.py                           # MongoDB abstraction layer
├── file_ops.py                           # File-system helpers (save, delete, replace)
├── logger.py                             # Shared rotating logger
└── run.py                                # Standalone CLI entry point
```

---

## Architecture

```
User input
    │
    ▼
run.py  ──── /help_vendor, /exit handled locally
    │         delete keyword → confirmation prompt
    │
    ▼
VendorsChatbot (agent.py / BaseAgent)
    │
    ├── Bare file path? ──────────────────────────► handle_upload()
    │
    ├── Slash command?  ──────────────────────────► _route_slash_command()
    │       ├── /upload_vendor  ──► handle_upload()
    │       ├── /list_vendor    ──► handle_list()
    │       ├── /get_vendor     ──► handle_get()
    │       ├── /delete_vendor  ──► handle_delete()
    │       ├── /update_vendor  ──► handle_update()
    │       └── /help_vendor    ──► HELP_TEXT
    │
    └── Natural language
            │
            ▼
        _detect_intent()   ← LlmAgent + intent_prompt (prompts.py)
            │               passes session document list so LLM can
            │               resolve "both", "the Dell one", etc.
            │
            └── _route_intent()
                    ├── upload  ──► handle_upload()
                    ├── list    ──► handle_list()
                    ├── get     ──► handle_get()
                    ├── delete  ──► handle_delete()   ← single / bulk / "all"
                    ├── update  ──► handle_update()
                    └── query   ──► handle_question() ← LlmAgent Q&A over docs
```

---

## File Reference

### `agent.py`

The central router. Contains two private intent-detection functions and two private routing methods.

**Intent detection functions:**

- **`_detect_intent(ctx, user_input, documents)`** — spins up a short-lived `LlmAgent` with `intent_prompt`, collects the JSON response, and returns a structured dict like `{"intent": "delete", "params": {"record_ids": "all"}}`.
- **`_parse_intent_response(raw, original_input)`** — two-attempt JSON parser with markdown-fence stripping and a safe fallback to `query` intent.

**Routing methods:**

- **`_route_slash_command(ctx, cmd, args, user_input)`** — handles all `/vendor`-suffixed commands and legacy aliases (`/upload`, `/list`, `/get`, `/delete`, `/update`, `/help`) without any LLM overhead.
- **`_route_intent(ctx, action, params, user_input)`** — handles the NL path, dispatching to the appropriate handler based on the detected action.

> Intent detection is self-contained in this file. There is no separate `intent.py`.

---

### `tools.py`

All stateful CRUD and Q&A handler functions. Each is an `async generator` that yields `Event` objects.

| Function | Signature | Description |
|---|---|---|
| `handle_upload` | `(ctx, file_path, author)` | Saves file, extracts text, structures with LLM, inserts to MongoDB, updates session |
| `handle_list` | `(ctx, author)` | Fetches all records from MongoDB and formats them |
| `handle_get` | `(ctx, record_ids, author)` | Accepts a list of IDs or `"all"` |
| `handle_delete` | `(ctx, record_ids, author)` | Accepts a list of IDs or `"all"` — deletes file, DB record, and session entry for each |
| `handle_update` | `(ctx, record_id, new_file_path, author)` | Replaces file, re-extracts text, re-structures, updates DB and session |
| `handle_question` | `(ctx, question, author)` | Loads docs from session (or DB fallback), answers via `LlmAgent` Q&A |
| `make_text_event` | `(author, text)` | Convenience factory — returns a plain-text `Event` |

**Key design detail — `STORAGE_DIR` is always absolute:**
```python
STORAGE_DIR = Path(__file__).resolve().parent / "VENDORS"
```
This ensures vendor documents are stored in `vendors_agent/VENDORS/` regardless of which directory Python is launched from.

---

### `prompts.py`

All LLM instruction strings and UI text in one place.

| Export | Purpose |
|---|---|
| `HELP_TEXT` | Printed to console on startup and `/help_vendor` |
| `intent_prompt(user_input, documents)` | Feeds the intent-detection LLM; includes session document list so the LLM can resolve contextual references like "the Dell one", "both of them" |
| `structuring_prompt(document_text)` | Instructs LLM to extract dynamic JSON from raw vendor document text |
| `vendor_qa_prompt(num_documents, doc_names, doc_context)` | Full Q&A system prompt including injected document content |
| `upload_success_message(...)` | Formatted upload confirmation string |
| `update_success_message(...)` | Formatted update confirmation string |
| `delete_success_message(...)` | Formatted delete confirmation string |

---

### `utils.py`

Pure helper functions with no agent dependencies.

| Function | Purpose |
|---|---|
| `is_file_path(text)` | Returns `True` if text is an existing supported file path |
| `extract_file_path(text)` | Finds the first valid file path embedded anywhere in a string (absolute or relative) |
| `extract_text(file_path)` | Extracts text from `.docx`, `.pdf`, `.txt`, `.md` including table data for PDFs |
| `parse_json_safely(text)` | Parses JSON with markdown-fence stripping and two-attempt fallback |
| `parse_command(user_input)` | Returns `(command, args_list)` or `(None, [])` for non-slash input |
| `format_record(rec)` | Formats a MongoDB record as a human-readable string |
| `build_document_context(documents)` | Builds the full text block injected into the Q&A prompt |
| `load_documents_from_state(raw)` | Safely deserialises session-state JSON to a dict |
| `session_entry_from_record(rec)` | Converts a MongoDB record into a session-state document entry |
| `validate_file_for_upload(file_path)` | Returns an error string if the file is missing or unsupported, else `None` |

---

### `run.py`

Standalone CLI entry point. Responsibilities:

- Adds `_AGENT_DIR` and `_ROOT_DIR` to `sys.path` so all imports resolve regardless of launch directory
- Bootstraps `Runner` and `InMemorySessionService`
- Handles `/help_vendor`, `/help`, and `/exit` locally before any agent call
- **Delete confirmation guard** — intercepts any message containing delete-related keywords (`delete`, `remove`, `erase`, `drop`, `wipe`, `purge`, `clear`) and asks for `yes/no` confirmation before forwarding to the agent
- Forwards everything else to `send_message()` → agent

---

## Setup

### 1. Install dependencies

```bash
pip install google-adk python-docx pdfplumber pymongo python-dotenv
```

### 2. Create `.env`

```env
# Google / Gemini
GOOGLE_API_KEY=your_google_api_key
MODEL=gemini-2.5-flash

# GCS
GOOGLE_APPLICATION_CREDENTIALS=path/to/service_account.json
BUCKET_NAME=your_bucket_name

# MongoDB
MONGO_URI=mongodb://localhost:27017
MONGO_DB=procure2pay_db

# Optional MongoDB auth
MONGO_USER=
MONGO_PASSWORD=
```

### 3. Ensure MongoDB is running

```bash
mongod --dbpath /data/db
```

---

## Running the Agent

From the `vendors_agent/` directory:

```bash
python run.py
```

Or from the `root_agent/` directory:

```bash
python vendors_agent/run.py
```

Both work because every file adds both `_AGENT_DIR` and `_ROOT_DIR` to `sys.path` at import time:

```python
_AGENT_DIR = Path(__file__).resolve().parent   # vendors_agent/
_ROOT_DIR  = _AGENT_DIR.parent                 # root_agent/
for _p in (_AGENT_DIR, _ROOT_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
```

---

## Usage Guide

### Upload a document

```
You: Upload docs/dell_india_vendor.docx
You: Add this vendor: C:/Vendors/supplier_abc.pdf
You: docs/dell_india_vendor.docx
```

Supported formats: `.docx` `.pdf` `.txt` `.md`

On upload the agent:
1. Copies the file into `VENDORS/` with a UTC timestamp prefix
2. Extracts all text (including tables for PDFs)
3. Calls an LLM to produce dynamic structured JSON (vendor name, contacts, pricing, certifications, payment terms, delivery terms, etc.)
4. Inserts both raw text and structured data into MongoDB
5. Caches the result in session state for fast Q&A

---

### List all records

```
You: Show all vendors
You: List everything in the database
You: What vendors do we have?
```

---

### View a specific record

```
You: Show details of the Dell vendor
You: Get record 6642abc123
You: What's in the first vendor document?
```

---

### Delete records

```
You: Delete the Dell vendor
You: Remove all vendors
You: Delete both of them
You: Delete record 6642abc123
```

The CLI will always ask for confirmation before any delete is forwarded to the agent:

```
  ⚠️  This looks like a delete operation.
  Deletions are permanent and cannot be undone.
  Confirm? (yes/no):
```

The agent resolves references like **"both of them"** or **"the Dell one"** using the list of documents currently known in the session.

---

### Update a record

```
You: Update the Dell vendor with docs/dell_v2.docx
You: Replace record 6642abc123 with docs/vendor_v2.docx
```

On update the agent:
1. Replaces the physical file in `VENDORS/`
2. Re-extracts text
3. Re-structures with LLM
4. Updates the MongoDB record
5. Refreshes the session cache

---

### Ask questions about vendor content

```
You: List all vendors that supply electronics
You: Which vendors are ISO certified?
You: What are the payment terms for Dell India?
You: Which vendor offers the lowest price for laptops?
You: Compare delivery lead times across all vendors
You: How many vendors supply home appliances?
You: Which vendors are marked as preferred?
You: What certifications do our vendors hold?
```

If no documents are in the current session, the agent automatically loads all records from MongoDB before answering.

---

## How Intent Detection Works

Every natural language message (that is not a bare file path or slash command) is passed to a short-lived `LlmAgent` via `_detect_intent()` in `agent.py`. The prompt (`intent_prompt` in `prompts.py`) includes:

- The full list of documents currently in the session (record ID, file name, position)
- Extraction rules for resolving contextual references
- Output format specification (strict JSON only)

The LLM returns one of:

```json
{"intent": "upload",  "params": {"file_path": "docs/dell_india_vendor.docx"}}
{"intent": "list",    "params": {}}
{"intent": "get",     "params": {"record_ids": ["abc123"]}}
{"intent": "delete",  "params": {"record_ids": "all"}}
{"intent": "delete",  "params": {"record_ids": ["abc123", "def456"]}}
{"intent": "update",  "params": {"record_id": "abc123", "new_file_path": "docs/v2.docx"}}
{"intent": "query",   "params": {"question": "Which vendor has the shortest lead time?"}}
```

If JSON parsing fails at both attempts, the agent falls back to `query` and passes the original input to the Q&A handler.

---

## Data Flow

```
User: "Delete both of them"
        │
        ▼
run.py  →  confirms deletion (yes/no)
        │
        ▼
agent.py  →  _detect_intent()
               │  intent_prompt includes:
               │    [1] record_id: abc123 | file_name: 20260421_043634_vendor_1.docx
               │    [2] record_id: def456 | file_name: 20260421_043701_vendor_2.docx
               ▼
             LLM returns:
               {"intent":"delete","params":{"record_ids":["abc123","def456"]}}
        │
        ▼
_route_intent()  →  handle_delete(ctx, ["abc123", "def456"], author)
  └── for each ID:
        ├── fetch from MongoDB
        ├── delete_file()  →  VENDORS/
        ├── delete_by_id() →  MongoDB
        └── remove from session state
        │
        ▼
Bot: "🗑 Delete complete — 2 record(s) processed."
```

---

## Storage

### File system

All uploaded files are stored in:
```
vendors_agent/VENDORS/<UTC_timestamp>_<original_filename>
```
Example:
```
vendors_agent/VENDORS/20260421_043634_vendor_1.docx
```

The path is always computed as:
```python
STORAGE_DIR = Path(__file__).resolve().parent / "VENDORS"
```

### MongoDB

Collection: `vendors`
Database: configured via `MONGO_DB` env var (default: `procure2pay_db`)

Each document has the following shape:

```json
{
  "_id":         "ObjectId",
  "file_name":   "20260421_043634_vendor_1.docx",
  "stored_path": "C:/...vendors_agent/VENDORS/20260421_043634_vendor_1.docx",
  "raw_text":    "Full extracted text...",
  "data": {
    "vendor_name":     "Dell India Pvt. Ltd.",
    "contact_details": { "...": "..." },
    "product_categories": ["Laptops", "Monitors", "Accessories"],
    "pricing":         { "...": "..." },
    "certifications":  ["ISO 9001", "ISO 27001"],
    "payment_terms":   "Net 30",
    "delivery_terms":  "7–10 business days"
  },
  "created_at":  "2026-04-21T03:56:34Z",
  "updated_at":  "2026-04-21T04:30:00Z"
}
```

---

## Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `GOOGLE_API_KEY` | ✅ | — | Gemini API key |
| `MODEL` | ✅ | `gemini-2.5-flash` | LLM model name |
| `GOOGLE_APPLICATION_CREDENTIALS` | ✅ | — | Path to GCS service account JSON |
| `BUCKET_NAME` | ✅ | — | GCS bucket name |
| `MONGO_URI` | ❌ | `mongodb://localhost:27017` | MongoDB connection URI |
| `MONGO_DB` | ❌ | `procure2pay_db` | MongoDB database name |
| `MONGO_USER` | ❌ | — | MongoDB username (auto-constructs URI) |
| `MONGO_PASSWORD` | ❌ | — | MongoDB password (auto-constructs URI) |

---

## Integration with Root Agent

`vendors_agent` is imported and used inside `root_agent/chatbot.py`. Because every file in the module adds both `_AGENT_DIR` and `_ROOT_DIR` to `sys.path`, imports resolve correctly whether the module is run standalone or imported from the parent:

```python
# root_agent/chatbot.py

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from vendors_agent.agent import vendors_agent
```

The `vendors_agent` export is the live `VendorsChatbot` instance, ready to be used as a sub-agent or tool within any ADK runner.
