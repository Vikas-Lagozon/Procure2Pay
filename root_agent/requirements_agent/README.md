# Requirements Agent

A **fully natural-language-driven** requirements management module built on top of Google ADK. Users interact in plain English to upload, list, view, update, and delete procurement requirement documents. The agent understands intent automatically — no slash commands required.

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
| Upload a document | `"Upload docs/laptop_req.docx"` |
| List all records | `"Show all requirements"` |
| View a record | `"Show details of the printer requirement"` |
| Delete one record | `"Delete the laptop requirement"` |
| Delete all records | `"Remove all requirements"` |
| Delete multiple | `"Delete both of them"` |
| Update a record | `"Replace the printer requirement with new_spec.docx"` |
| Ask questions | `"How many units of laptops do we need?"` |
| Filter by category | `"List all requirements related to home appliances"` |
| Cross-doc analysis | `"Which department has the most requirements?"` |

---

## Directory Structure

```
requirements_agent/
├── REQUIREMENTS/                         # Physical document storage
│   ├── 20260421_032541_laptop_req.docx
│   └── 20260421_032851_printer_req.docx
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
run.py  ──── /help, /exit handled locally
    │         delete keyword → confirmation prompt
    │
    ▼
RequirementsChatbot (agent.py / BaseAgent)
    │
    ├── Bare file path? ──────────────────────────► handle_upload()
    │
    └── Natural language
            │
            ▼
        _detect_intent()   ← LlmAgent + intent_prompt (prompts.py)
            │               passes session document list so LLM can
            │               resolve "both", "the laptop one", etc.
            │
            ├── upload  ──► handle_upload()
            ├── list    ──► handle_list()
            ├── get     ──► handle_get()
            ├── delete  ──► handle_delete()   ← supports single / bulk / "all"
            ├── update  ──► handle_update()
            └── query   ──► handle_question() ← LlmAgent Q&A over document context
```

---

## File Reference

### `agent.py`
The central router. Contains two private functions:

- **`_detect_intent(ctx, user_input, documents)`** — spins up a short-lived `LlmAgent` with `intent_prompt`, collects the JSON response, and returns a structured dict like `{"intent": "delete", "params": {"record_ids": "all"}}`.
- **`_parse_intent_response(raw, original_input)`** — two-attempt JSON parser with safe fallback to `query` intent.
- **`RequirementsChatbot._run_async_impl`** — routes based on detected intent to the appropriate handler in `tools.py`.

> Intent detection is self-contained in this file. There is no separate `intent.py`.

---

### `tools.py`
All stateful CRUD and Q&A handler functions. Each is an `async generator` that yields `Event` objects.

| Function | Description |
|---|---|
| `handle_upload(author, ctx, file_path)` | Saves file, extracts text, structures with LLM, inserts to MongoDB, updates session |
| `handle_list(author, ctx)` | Fetches all records from MongoDB and formats them |
| `handle_get(author, ctx, record_ids)` | Accepts a list of IDs or `"all"` |
| `handle_delete(author, ctx, record_ids)` | Accepts a list of IDs or `"all"` — deletes file, DB record, and session entry for each |
| `handle_update(author, ctx, record_id, new_file_path)` | Replaces file, re-extracts text, re-structures, updates DB and session |
| `handle_question(author, ctx, question)` | Loads docs from session (or DB fallback), answers via `LlmAgent` Q&A |

**Key design detail — `STORAGE_DIR` is always absolute:**
```python
STORAGE_DIR = Path(__file__).resolve().parent / "REQUIREMENTS"
```
This ensures documents are stored in `requirements_agent/REQUIREMENTS/` regardless of which directory Python is launched from.

---

### `prompts.py`
All LLM instruction strings in one place.

| Export | Purpose |
|---|---|
| `HELP_TEXT` | Printed to console on startup and `/help` |
| `intent_prompt(user_input, documents)` | Feeds the intent-detection LLM; includes session document list so the LLM can resolve contextual references |
| `structurer_prompt(text_content)` | Instructs LLM to extract dynamic JSON from raw document text |
| `qa_prompt(documents, doc_context)` | Full Q&A system prompt including injected document content |

---

### `utils.py`
Pure helper functions with no agent dependencies.

| Function | Purpose |
|---|---|
| `extract_text(file_path)` | Extracts text from `.docx`, `.pdf`, `.txt`, `.md` |
| `parse_json_safely(text)` | Parses JSON with markdown-fence stripping and fallback |
| `is_file_path(text)` | Returns `True` if text is an existing supported file path |
| `extract_file_path(text)` | Finds the first valid file path embedded in any string |
| `format_record(rec)` | Formats a MongoDB record as a human-readable string |
| `build_document_context(documents)` | Builds the full text block injected into the Q&A prompt |

---

### `run.py`
Standalone CLI entry point. Responsibilities:

- Bootstraps `Runner` and `InMemorySessionService`
- Handles `/help` and `/exit` locally before any agent call
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

From the `requirements_agent/` directory:

```bash
python run.py
```

Or from the `root_agent/` directory:

```bash
python requirements_agent/run.py
```

Both work because every file adds both `_AGENT_DIR` and `_ROOT_DIR` to `sys.path` at import time:

```python
_AGENT_DIR = Path(__file__).resolve().parent
_ROOT_DIR  = _AGENT_DIR.parent
for _p in (_AGENT_DIR, _ROOT_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
```

---

## Usage Guide

### Upload a document

```
You: Upload docs/laptop_requirement.docx
You: Add this requirement: C:/Requirements/printer_spec.pdf
You: docs/laptop_requirement.docx
```

Supported formats: `.docx` `.pdf` `.txt` `.md`

On upload the agent:
1. Copies the file into `REQUIREMENTS/` with a UTC timestamp prefix
2. Extracts all text (including tables for PDFs)
3. Calls an LLM to produce a dynamic structured JSON
4. Inserts both into MongoDB
5. Caches the result in session state for fast Q&A

---

### List all records

```
You: Show all requirements
You: List everything in the database
You: What requirements do we have?
```

---

### View a specific record

```
You: Show details of the laptop requirement
You: Get record 6642abc123
You: What's in the first requirement?
```

---

### Delete records

```
You: Delete the laptop requirement
You: Remove all requirements
You: Delete both of them
You: Delete record 6642abc123
```

The CLI will always ask for confirmation before any delete is forwarded to the agent:

```
  ⚠️  This looks like a delete operation.
  Deletions are permanent and cannot be undone.
  Confirm? (yes/no):
```

The agent resolves references like **"both of them"** or **"the laptop one"** using the list of documents currently known in the session.

---

### Update a record

```
You: Update the printer requirement with docs/printer_v2.docx
You: Replace record 6642abc123 with docs/req_v2.docx
```

On update the agent:
1. Replaces the physical file in `REQUIREMENTS/`
2. Re-extracts text
3. Re-structures with LLM
4. Updates the MongoDB record
5. Refreshes the session cache

---

### Ask questions about requirement content

```
You: Show me all technical requirements
You: List requirements related to home appliances
You: How many requirements are for electronics items?
You: How many units of laptops do we need, and in which requirements?
You: What is the ideal specification of all printer requirements?
You: Which department has the most requirements?
You: Compare technical requirements across all documents
```

If no documents are in the current session, the agent automatically loads all records from MongoDB before answering.

---

## How Intent Detection Works

Every natural language message (that is not a bare file path) is passed to a short-lived `LlmAgent` via `_detect_intent()` in `agent.py`. The prompt (`intent_prompt` in `prompts.py`) includes:

- The full list of documents currently in the session (record ID, file name, position)
- Extraction rules for resolving contextual references
- Output format specification (strict JSON only)

The LLM returns one of:

```json
{"intent": "upload",  "params": {"file_path": "docs/req.docx"}}
{"intent": "list",    "params": {}}
{"intent": "get",     "params": {"record_ids": ["abc123"]}}
{"intent": "delete",  "params": {"record_ids": "all"}}
{"intent": "delete",  "params": {"record_ids": ["abc123", "def456"]}}
{"intent": "update",  "params": {"record_id": "abc123", "new_file_path": "docs/v2.docx"}}
{"intent": "query",   "params": {"question": "How many laptops do we need?"}}
```

If JSON parsing fails, the agent falls back to `query` and passes the original input to the Q&A handler.

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
               │    [1] record_id: abc123 | file_name: laptop_req.docx
               │    [2] record_id: def456 | file_name: printer_req.docx
               ▼
             LLM returns:
               {"intent":"delete","params":{"record_ids":["abc123","def456"]}}
        │
        ▼
tools.handle_delete(ctx, ["abc123", "def456"])
  └── for each ID:
        ├── fetch from MongoDB
        ├── delete_file()  →  REQUIREMENTS/
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
requirements_agent/REQUIREMENTS/<UTC_timestamp>_<original_filename>
```
Example:
```
requirements_agent/REQUIREMENTS/20260421_032541_laptop_requirement_1.docx
```

The path is always computed as:
```python
STORAGE_DIR = Path(__file__).resolve().parent / "REQUIREMENTS"
```

### MongoDB

Collection: `requirements`  
Database: configured via `MONGO_DB` env var (default: `procure2pay_db`)

Each document has the following shape:

```json
{
  "_id":         "ObjectId",
  "file_name":   "20260421_032541_laptop_requirement_1.docx",
  "stored_path": "C:/...requirements_agent/REQUIREMENTS/20260421_032541_laptop_requirement_1.docx",
  "raw_text":    "Full extracted text...",
  "data":        { "...dynamic structured JSON extracted by LLM..." },
  "created_at":  "2026-04-21T03:25:41Z",
  "updated_at":  "2026-04-21T04:00:00Z"
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

`requirements_agent` is imported and used inside `root_agent/chatbot.py`. Because every file in the module adds both `_AGENT_DIR` and `_ROOT_DIR` to `sys.path`, imports resolve correctly whether the module is run standalone or imported from the parent:

```python
# root_agent/chatbot.py

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from requirements_agent.agent import requirements_agent
```

The `requirements_agent` export is the live `RequirementsChatbot` instance, ready to be used as a sub-agent or tool within any ADK runner.
