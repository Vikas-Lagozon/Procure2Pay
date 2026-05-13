# vendors_agent/prompts.py
"""
All prompts, system instructions, and static text constants
used by the Vendors Agent.

Nothing in this module contains business logic — it is purely
a centralised store for strings so that agent.py, tools.py,
and utils.py stay free of hardcoded prose.
"""

from __future__ import annotations


# ─────────────────────────────────────────────────────────────
# HELP TEXT
# ─────────────────────────────────────────────────────────────

HELP_TEXT: str = """
📖 Vendors Chatbot — Natural Language Interface
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Just talk naturally — no commands required.

UPLOAD
  "Upload docs/dell_india_vendor.docx"
  "Add this vendor: C:/Vendors/supplier_abc.pdf"

LIST
  "Show all vendors"
  "List everything in the database"

VIEW DETAILS
  "Show details of the Dell vendor"
  "Get record 6642abc123"

DELETE
  "Delete the Dell vendor"
  "Remove all vendors"
  "Delete both of them"

UPDATE
  "Update the Dell vendor with dell_v2.docx"
  "Replace record 6642abc123 with docs/vendor_v2.docx"

ASK QUESTIONS
  "List all vendors that supply electronics"
  "Which vendors are ISO certified?"
  "What are the payment terms for Dell India?"
  "Which vendor offers the lowest price for laptops?"
  "Compare delivery lead times across all vendors"

TYPE /help_vendor to see this again, /exit to quit.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""


# ─────────────────────────────────────────────────────────────
# INTENT DETECTION PROMPT
# ─────────────────────────────────────────────────────────────

def intent_prompt(user_input: str, documents: dict) -> str:
    """
    Build the prompt fed to the intent-detection LlmAgent.

    Passes the full session document list so the LLM can resolve
    contextual references like "both of them", "the Dell one", etc.
    """
    doc_lines = ""
    for i, (original_name, doc) in enumerate(documents.items(), 1):
        doc_lines += (
            f"  [{i}] record_id : {doc.get('record_id', 'unknown')}\n"
            f"       file_name : {doc.get('file_name', original_name)}\n"
            f"       key       : {original_name}\n"
        )

    return f"""You are an intent detection engine for a Vendor Management chatbot.

Analyse the user message and return ONE JSON object — nothing else.

━━━ VENDOR DOCUMENTS CURRENTLY IN SESSION ━━━
{doc_lines.strip() if doc_lines.strip() else "  (none loaded yet — DB will be queried at runtime)"}

━━━ POSSIBLE INTENTS ━━━
upload  — user wants to upload / add a vendor document
list    — user wants to see all vendor records
get     — user wants to view details of one or more specific records
delete  — user wants to delete one, several, or all records
update  — user wants to replace / update a record's file
query   — user wants to ask a question about vendor CONTENT

━━━ EXTRACTION RULES ━━━
1. FILE PATH  — extract any token ending in .docx / .pdf / .txt / .md
2. "all", "both", "everything", "all of them"  → record_ids = "all"
3. "the Dell one", "Dell vendor", "printer supplier"
   → resolve to the matching record_id from DOCUMENTS IN SESSION above
4. "first one", "second one"  → resolve by [position] in the list above
5. If user says something like "delete_vendor <filename>"
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
# STRUCTURING PROMPT
# ─────────────────────────────────────────────────────────────

def structuring_prompt(document_text: str) -> str:
    """
    Build the instruction sent to the dynamic-structurer LLM agent.
    """
    return f"""You are an intelligent document parser specialised in vendor documents.

Extract ALL meaningful structured information from the vendor document.

Rules:
- Do NOT use a fixed schema
- Create dynamic JSON keys that reflect the actual content
- Preserve hierarchy where appropriate
- Always try to extract: vendor name, contact details, product categories,
  pricing, certifications, payment terms, delivery terms, and any other
  key commercial or logistical data present in the document

Return ONLY valid JSON — no markdown fences, no explanation.

DOCUMENT:
{document_text[:12000]}
"""


# ─────────────────────────────────────────────────────────────
# VENDOR Q&A PROMPT
# ─────────────────────────────────────────────────────────────

def vendor_qa_prompt(
    num_documents: int,
    doc_names: list[str],
    doc_context: str,
) -> str:
    """
    Build the system instruction for the vendor QA LlmAgent.
    """
    names_block = "\n".join(f"  - {n}" for n in doc_names)
    return f"""You are an expert Vendor Management assistant with deep knowledge \
of procurement and supplier management.

You have access to {num_documents} vendor document(s):
{names_block}

You can answer ANY natural language question about these documents, including:

LOOKUP & DETAIL questions
  - "What are the contact details of Dell India?"
  - "What products does the printer vendor supply?"
  - "Show me the payment terms for vendor XYZ."

FILTERING & CATEGORY questions
  - "List all vendors that supply electronics."
  - "Which vendors provide home appliances?"
  - "Show me all vendors with ISO certification."
  - "Which vendors are marked as preferred?"

COUNTING & AGGREGATION questions
  - "How many vendors are registered in total?"
  - "How many vendors supply laptops?"
  - "How many vendors offer credit payment terms?"

PRICING & COMMERCIAL questions
  - "What is the unit price offered by each vendor for laptops?"
  - "Which vendor offers the best pricing for network switches?"
  - "List all vendors with a minimum order quantity above 10 units."

CROSS-DOCUMENT ANALYSIS questions
  - "Compare the warranty terms across all vendors."
  - "Which vendor has the shortest delivery lead time?"
  - "What certifications do our vendors hold?"

GUIDELINES:
- Always cite WHICH document your answer comes from.
- For counting/aggregation, scan ALL documents and sum where relevant.
- For filtering, scan ALL documents and return only matching ones.
- If a question spans multiple documents, address each one then give a combined summary.
- If the answer is not found in any document, say so clearly.
- Present answers in a clean, structured format (use bullet points or tables where helpful).

--- VENDOR DOCUMENTS CONTEXT ---
{doc_context}
"""


# ─────────────────────────────────────────────────────────────
# UPLOAD SUCCESS MESSAGE
# ─────────────────────────────────────────────────────────────

def upload_success_message(
    original_name: str,
    inserted_id: str,
    text_length: int,
    num_docs_in_session: int,
    doc_list: str,
) -> str:
    return (
        f"✅ Vendor document uploaded successfully.\n\n"
        f"File     : {original_name}\n"
        f"Record ID: {inserted_id}\n"
        f"Length   : {text_length} characters\n\n"
        f"📂 Vendor documents loaded in this session ({num_docs_in_session}):\n"
        f"{doc_list}\n\n"
        f"You can now ask questions about any of these vendor documents."
    )


# ─────────────────────────────────────────────────────────────
# UPDATE SUCCESS MESSAGE
# ─────────────────────────────────────────────────────────────

def update_success_message(
    old_file_name: str,
    file_msg: str,
    db_msg: str,
    session_msg: str,
    new_text_length: int,
    structured_keys: list[str],
) -> str:
    return (
        f"🔄 Update complete for: {old_file_name}\n\n"
        f"{file_msg}\n"
        f"{db_msg}\n"
        f"{session_msg}\n\n"
        f"New content length : {new_text_length} characters\n"
        f"Structured keys    : {structured_keys}\n\n"
        f"You can now ask questions about the updated vendor document."
    )


# ─────────────────────────────────────────────────────────────
# DELETE SUCCESS MESSAGE
# ─────────────────────────────────────────────────────────────

def delete_success_message(
    file_name: str,
    db_msg: str,
    file_msg: str,
    session_msg: str,
) -> str:
    return (
        f"🗑  Delete complete for: {file_name}\n\n"
        f"{db_msg}\n"
        f"{file_msg}\n"
        f"{session_msg}"
    )
