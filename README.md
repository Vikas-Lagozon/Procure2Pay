# Procure2Pay — Technical Reference

## Overview

Procure2Pay is an AI-driven procurement automation system built on Google ADK (Agent Development Kit) and Gemini LLM. It automates the four-step procurement lifecycle: requirement ingestion, vendor management, intelligent vendor matching, and result retrieval.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                          FastAPI (app.py)                           │
│  REST endpoints + SSE streaming + session management                │
└────────────────────────────┬────────────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    ADK Runner + LlmAgent (chatbot.py)               │
│  Model: Gemini 2.5 Flash                                            │
│  Tools: upload_requirement | upload_vendor | match_vendors |        │
│         fetch_results                                               │
└────┬───────────┬────────────────┬─────────────────┬────────────────┘
     │           │                │                 │
     ▼           ▼                ▼                 ▼
 requirements  vendors        matcher.py       matcher.py
    .py          .py          (scoring)      (fetch_results)
     │           │                │
     └─────┬─────┘                │
           │                      │
           ▼                      ▼
   ┌───────────────────────────────────────┐
   │             MongoDB                   │
   │  collections:                         │
   │    requirements   — ingested docs     │
   │    vendors        — vendor profiles   │
   │    matched_results — top-5 rankings   │
   └───────────────────────────────────────┘
           │
   ┌───────┴──────────┐
   │  Local File Store │
   │  REQUIREMENT/     │
   │  VENDOR/          │
   └───────────────────┘
           │
   ┌───────────────────┐
   │   PostgreSQL       │
   │  (ADK sessions)   │
   └───────────────────┘
```

---

## System Flow

### Step 1 — Requirement Upload

```
User file (PDF/DOCX)
  → validate + copy to REQUIREMENT/<uuid>.<ext>
  → Gemini LLM: extract structured fields
  → MongoDB requirements collection: insert document
  → return requirement_id
```

Extracted fields: `title`, `description`, `budget`, `timeline`, `required_services`, `location`, `created_at`

---

### Step 2 — Vendor Management

**Add Vendor:**
```
User file (PDF/DOCX)
  → validate + copy to VENDOR/<uuid>.<ext>
  → Gemini LLM: extract structured profile
  → MongoDB vendors collection: insert document
  → return vendor_id
```

Extracted fields: `vendor_name`, `services`, `experience_years`, `pricing_model`, `location`, `rating`, `past_projects`

**Fetch Vendors:**
```
GET /vendors → MongoDB vendors.fetch_all() → JSON list
```

---

### Step 3 — Vendor Matching

```
requirement_id
  → fetch requirement from MongoDB
  → fetch all vendors from MongoDB
  → score each vendor (5 dimensions)
  → rank descending, slice top 5
  → upsert into MongoDB matched_results collection
  → return MatchResult
```

**Scoring Weights:**

| Dimension           | Weight | Logic                                          |
|---------------------|--------|------------------------------------------------|
| Service Match       | 40 pts | Jaccard overlap: matched_services / req_services |
| Budget Compatibility| 20 pts | Keyword heuristics + numeric ratio             |
| Location Match      | 15 pts | Exact string match → token overlap             |
| Experience          | 15 pts | Linear scale: 0 yrs = 0, 10+ yrs = 15         |
| Rating              | 10 pts | Normalised from 0–5 or 0–10 scale             |

Total possible score: **100 pts**

---

### Step 4 — Fetch Results

```
requirement_id
  → MongoDB matched_results.fetch_one({requirement_id})
  → return stored top-5 vendor list with scores and reasons
```

Re-running a match (`POST /match/{id}`) overwrites the stored result (upsert by `requirement_id`).

---

## MongoDB Schema

### `requirements` collection

```json
{
  "_id":               "ObjectId (hex string)",
  "file_name":         "original_filename.pdf",
  "stored_name":       "<uuid>.pdf",
  "file_path":         "/abs/path/REQUIREMENT/<uuid>.pdf",
  "file_type":         "PDF",
  "upload_timestamp":  "2025-01-01T00:00:00+00:00",
  "title":             "Cloud Infrastructure Procurement",
  "description":       "...",
  "budget":            "$500,000",
  "timeline":          "Q3 2025",
  "required_services": ["cloud hosting", "devops", "security"],
  "location":          "New York, USA",
  "created_at":        "2025-01-01T00:00:00+00:00"
}
```

### `vendors` collection

```json
{
  "_id":               "ObjectId (hex string)",
  "file_name":         "vendor_profile.docx",
  "stored_name":       "<uuid>.docx",
  "file_path":         "/abs/path/VENDOR/<uuid>.docx",
  "file_type":         "DOCX",
  "upload_timestamp":  "2025-01-01T00:00:00+00:00",
  "vendor_name":       "TechCorp Solutions",
  "services":          ["cloud hosting", "devops", "monitoring"],
  "experience_years":  "8",
  "pricing_model":     "flexible, per-project",
  "location":          "New York, USA",
  "rating":            "4.5",
  "past_projects":     ["Project Alpha", "Project Beta"]
}
```

### `matched_results` collection

```json
{
  "_id":            "ObjectId (hex string)",
  "requirement_id": "<hex string of requirements._id>",
  "matched_at":     "2025-01-01T00:00:00+00:00",
  "vendor_count":   5,
  "top_vendors": [
    {
      "rank":             1,
      "vendor_id":        "<hex string>",
      "vendor_name":      "TechCorp Solutions",
      "score":            87.5,
      "reason":           "Matches 3 required service(s): ...",
      "services":         ["Cloud Hosting", "Devops"],
      "experience_years": "8",
      "pricing_model":    "flexible, per-project",
      "location":         "New York, USA",
      "rating":           "4.5",
      "summary":          "Score 87.5/100 — svc:32 bgt:18 loc:15 exp:12 rat:9"
    }
  ],
  "table_data": []
}
```

---

## File Structure

```
procure2pay/
│
├── app.py              # FastAPI application — all REST endpoints + SSE
├── chatbot.py          # ADK agent setup, Runner, session management, chat_stream
├── tools.py            # Agent tool wrappers (4 tools)
├── requirements.py     # Requirement ingestion: validate → store → extract → persist
├── vendors.py          # Vendor ingestion: validate → store → extract → persist
├── matcher.py          # Scoring engine + matched_results persistence + fetch
├── nosql_db.py         # MongoDB abstraction (MongoDBConnection + MongoCollection)
├── llm_utils.py        # Gemini API wrapper: extract_data_from_doc()
├── config.py           # All environment config (Gemini, MongoDB, PostgreSQL, GCS)
├── logger.py           # Rotating daily file logger + console handler
├── run.py              # CLI entry point (interactive terminal chat)
│
├── REQUIREMENT/        # Stored requirement files (auto-created)
├── VENDOR/             # Stored vendor files (auto-created)
├── tmp_uploads/        # Temp dir for multipart file uploads (auto-created)
├── logs/               # Log files (auto-created)
│
├── static/             # Static assets for UI (optional)
├── templates/          # Jinja2 HTML templates (optional)
│
├── .env                # Environment variables (see below)
└── README.md
```

---

## Environment Variables (`.env`)

```env
# ── Gemini ────────────────────────────────────────────────────
GOOGLE_API_KEY=your_gemini_api_key
MODEL=gemini-2.5-flash

# ── GCS (file storage artifacts) ──────────────────────────────
GOOGLE_APPLICATION_CREDENTIALS=/path/to/service_account.json
BUCKET_NAME=your_gcs_bucket_name

# ── MongoDB ───────────────────────────────────────────────────
MONGO_URI=mongodb://localhost:27017
MONGO_DB=procure2pay_db
MONGO_USER=                    # optional
MONGO_PASSWORD=                # optional

# ── PostgreSQL (ADK session store) ────────────────────────────
PG_USER=procure2pay
PG_PASSWORD=abcd1234
PG_HOST=localhost
PG_PORT=5432
PG_DB=procure2pay_db
DB_SCHEMA=public

# ── BigQuery (optional analytics) ────────────────────────────
BQ_PROJECT_ID=
BQ_DATASET=
SERVICE_ACCOUNT_FILE=
```

---

## REST API Reference

### Step 1 — Requirements

| Method | Endpoint             | Description                       |
|--------|----------------------|-----------------------------------|
| POST   | `/upload/requirement`| Upload requirement PDF/DOCX       |
| GET    | `/requirements`      | List all requirements in MongoDB  |

**POST /upload/requirement** — multipart form, field: `file`

Response:
```json
{
  "success": true,
  "requirement_id": "64f1a2b3c4d5e6f7a8b9c0d1",
  "message": "...",
  "extracted_fields": { "title": "...", "budget": "...", ... }
}
```

---

### Step 2 — Vendors

| Method | Endpoint        | Description                    |
|--------|-----------------|--------------------------------|
| POST   | `/upload/vendor`| Upload vendor PDF/DOCX         |
| GET    | `/vendors`      | List all vendors in MongoDB    |

---

### Step 3 — Matching

| Method | Endpoint                    | Description                           |
|--------|-----------------------------|---------------------------------------|
| POST   | `/match/{requirement_id}`   | Run matching, persist top-5 results   |

Response includes top 5 vendors with `rank`, `score`, `reason`, and all profile fields.

---

### Step 4 — Results

| Method | Endpoint                        | Description                              |
|--------|---------------------------------|------------------------------------------|
| GET    | `/results/{requirement_id}`     | Fetch stored match results               |
| GET    | `/results`                      | List all stored match result summaries   |

---

### Chat

| Method | Endpoint        | Description                          |
|--------|-----------------|--------------------------------------|
| GET    | `/chat/stream`  | SSE streaming chat (`user_input`, `session_id` query params) |
| POST   | `/new_chat`     | Create new chat session              |
| PATCH  | `/session/{id}/rename` | Rename session                |
| DELETE | `/session/{id}` | Delete session                       |
| GET    | `/session/{id}/history` | Fetch session message history |

---

### System

| Method | Endpoint   | Description                                        |
|--------|------------|----------------------------------------------------|
| GET    | `/health`  | MongoDB + session service health + collection counts |
| GET    | `/`        | UI home page (requires `templates/index.html`)     |

---

## ADK Agent Tools

| Tool                | Parameters                          | Returns |
|---------------------|-------------------------------------|---------|
| `upload_requirement`| `file_path: str`                    | Formatted summary + `requirement_id` |
| `upload_vendor`     | `file_path: str`                    | Formatted summary + `vendor_id` |
| `match_vendors`     | `requirement_id: str`               | Top-5 ranked report + ASCII table + JSON |
| `fetch_results`     | `requirement_id: str`, `all_results: bool` | Stored results from `matched_results` |

---

## LLM Integration

**Module:** `llm_utils.py` — `extract_data_from_doc(file_path, schema, domain_hint)`

**Flow:**
1. Read raw text from PDF (`pypdf`) or DOCX (`python-docx`)
2. Build a structured extraction prompt embedding the target JSON schema
3. Call Gemini with `temperature=0.1`, `max_output_tokens=1500`
4. Strip markdown fences, parse and return JSON

**Document text cap:** First 12,000 characters are sent to the LLM.

---

## Dependencies

```
google-adk
google-generativeai
pymongo
motor                  # async mongo (optional)
fastapi
uvicorn[standard]
python-dotenv
certifi
pypdf
python-docx
sqlalchemy
asyncpg
google-cloud-storage   # optional, for GCS artifact service
httpx
jinja2
python-multipart
```

Install:
```bash
pip install -r requirements.txt
```

---

## Running the System

**FastAPI server:**
```bash
uvicorn app:app --host 0.0.0.0 --port 8000 --reload
```

**CLI chatbot (interactive terminal):**
```bash
python run.py
```

---

## Session Storage

ADK sessions are persisted in PostgreSQL via `DatabaseSessionService` (SQLAlchemy + asyncpg). The schema is managed by ADK internally under the `DB_SCHEMA` namespace (default: `public`).

---

## Logging

All modules use the shared logger from `logger.py`. Logs write to:
- `logs/log_<YYYYMMDD_HHMMSS>.log` — DEBUG and above
- stdout — INFO and above

Format: `YYYY-MM-DD HH:MM:SS | LEVEL    | module.name | message`
