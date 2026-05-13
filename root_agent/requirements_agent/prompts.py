# requirements_agent/prompts.py

# ─────────────────────────────────────────────────────────────
# HELP TEXT
# ─────────────────────────────────────────────────────────────

HELP_TEXT = """
📖 Requirements Chatbot — Natural Language Interface
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Just talk naturally — no commands required.

UPLOAD
  "Upload docs/req_v1.docx"
  "Add this requirement: C:/files/laptops.pdf"

LIST
  "Show all requirements"
  "List everything in the database"

VIEW DETAILS
  "Show details of the laptop requirement"
  "Get record 6642abc123"

DELETE
  "Delete the laptop requirement"
  "Remove all requirements"
  "Delete both of them"

UPDATE
  "Update the printer requirement with new_spec.docx"
  "Replace record 6642abc123 with docs/req_v2.docx"

ASK QUESTIONS
  "Show me all technical requirements"
  "How many units of laptops do we need?"
  "Which department has the most requirements?"
  "What is the ideal spec for all printer requirements?"

TYPE /help to see this again, /exit to quit.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""


# ─────────────────────────────────────────────────────────────
# INTENT DETECTION PROMPT
# ─────────────────────────────────────────────────────────────

def intent_prompt(user_input: str, documents: dict) -> str:
    doc_lines = ""
    for i, (original_name, doc) in enumerate(documents.items(), 1):
        doc_lines += (
            f"  [{i}] record_id : {doc.get('record_id', 'unknown')}\n"
            f"       file_name : {doc.get('file_name', original_name)}\n"
            f"       key       : {original_name}\n"
        )

    return f"""You are an intent detection engine for a Requirements Management chatbot.

Analyse the user message and return ONE JSON object — nothing else.

━━━ DOCUMENTS CURRENTLY IN SESSION ━━━
{doc_lines.strip() if doc_lines.strip() else "  (none loaded yet — DB will be queried at runtime)"}

━━━ POSSIBLE INTENTS ━━━
upload  — user wants to upload / add a requirement document
list    — user wants to see all requirement records
get     — user wants to view details of one or more specific records
delete  — user wants to delete one, several, or all records
update  — user wants to replace / update a record's file
query   — user wants to ask a question about requirement CONTENT

━━━ EXTRACTION RULES ━━━
1. FILE PATH  — extract any token ending in .docx / .pdf / .txt / .md
2. "all", "both", "everything", "all of them"  → record_ids = "all"
3. "the laptop one", "laptop requirement", "printer document"
   → resolve to the matching record_id from DOCUMENTS IN SESSION above
4. "first one", "second one"  → resolve by [position] in the list above
5. If user says something like "delete_requirement <filename>"
   → intent = delete, resolve filename to record_id if possible
6. update needs BOTH a record identifier AND a new file path
7. Default fallback when nothing matches: intent = query

━━━ OUTPUT FORMAT (return ONLY this JSON, no markdown, no explanation) ━━━

For upload:
{{"intent":"upload","params":{{"file_path":"<path>"}}}}

For list:
{{"intent":"list","params":{{}}}}

For get — one or many:
{{"intent":"get","params":{{"record_ids":["<id1>","<id2>"]}}}}
{{"intent":"get","params":{{"record_ids":"all"}}}}

For delete — one, many, or all:
{{"intent":"delete","params":{{"record_ids":["<id1>","<id2>"]}}}}
{{"intent":"delete","params":{{"record_ids":"all"}}}}

For update:
{{"intent":"update","params":{{"record_id":"<id>","new_file_path":"<path>"}}}}

For query:
{{"intent":"query","params":{{"question":"{user_input}"}}}}

━━━ USER MESSAGE ━━━
{user_input}
"""


# ─────────────────────────────────────────────────────────────
# DOCUMENT STRUCTURER PROMPT
# ─────────────────────────────────────────────────────────────

def structurer_prompt(text_content: str) -> str:
    return f"""You are an intelligent document parser.

Extract ALL meaningful structured information from the document.

Rules:
- Do NOT use a fixed schema
- Create dynamic JSON based on content
- Preserve hierarchy
- Extract entities, metadata, tables, lists, and key-value pairs

Return ONLY valid JSON.

DOCUMENT:
{text_content[:12000]}
"""


# ─────────────────────────────────────────────────────────────
# Q&A PROMPT
# ─────────────────────────────────────────────────────────────

def requirement_qa_prompt(documents: dict, doc_context: str) -> str:
    doc_names = list(documents.keys())
    return f"""You are an expert Requirements Analyst assistant with deep knowledge of \
procurement and business requirements.

You have access to {len(documents)} requirement document(s):
{chr(10).join([f"  - {n}" for n in doc_names])}

Answer ANY natural language question about these documents, including:
  - Lookup & detail questions
  - Filtering & category questions
  - Counting & aggregation questions
  - Cross-document analysis
  - Unit & quantity questions

GUIDELINES:
- Always cite WHICH document your answer comes from.
- For counting/aggregation, scan ALL documents and sum up.
- For filtering, return only matching items across all documents.
- Present answers in a clean, structured format.
- If an answer is not found, say so clearly.

--- DOCUMENTS CONTEXT ---
{doc_context}
"""
