# requirements_agent/prompts.py
# All prompt templates for the Requirements Agent.

from __future__ import annotations


# ─────────────────────────────────────────────────────────────
# HELP TEXT
# ─────────────────────────────────────────────────────────────

HELP_TEXT = """
Requirements Chatbot — Natural Language Interface

Just talk naturally — no commands required.

UPLOAD    "Upload docs/req_v1.docx"
LIST      "Show all requirements"
VIEW      "Show details of the laptop requirement"  /  "Get record <id>"
DELETE    "Delete the laptop requirement"  /  "Remove all requirements"
UPDATE    "Update the printer requirement with new_spec.docx"
QUERY     "How many units of laptops do we need?"
          "Which department has the most requirements?"
          "What is the ideal spec for all printer requirements?"

Type /help to see this again, /exit to quit.
"""


# ─────────────────────────────────────────────────────────────
# INTENT DETECTION PROMPT
# ─────────────────────────────────────────────────────────────

def intent_prompt(user_input: str, documents: dict) -> str:
    doc_lines = ""
    for i, (original_name, doc) in enumerate(documents.items(), 1):
        doc_lines += (
            f"  [{i}] record_id={doc.get('record_id', 'unknown')} "
            f"file={doc.get('file_name', original_name)} key={original_name}\n"
        )

    return f"""You are an intent detection engine for a Requirements Management chatbot.
Return ONE JSON object — nothing else.

REQUIREMENT DOCUMENTS IN SESSION:
{doc_lines.strip() if doc_lines.strip() else "  (none — DB will be queried at runtime)"}

INTENTS: upload | list | get | delete | update | query

RULES:
1. Extract file path from any token ending in .docx / .pdf / .txt / .md
2. "all", "both", "everything", "all of them" -> record_ids = "all"
3. Requirement references like "the laptop one" -> resolve to record_id from session list above
4. "first one", "second one" -> resolve by [position] in session list
5. update needs BOTH a record identifier AND a new file path
6. Default fallback: intent = query

OUTPUT (return ONLY this JSON):

upload:  {{"intent":"upload","params":{{"file_path":"<path>"}}}}
list:    {{"intent":"list","params":{{}}}}
get:     {{"intent":"get","params":{{"record_ids":["<id1>"]}}}}
         {{"intent":"get","params":{{"record_ids":"all"}}}}
delete:  {{"intent":"delete","params":{{"record_ids":["<id1>"]}}}}
         {{"intent":"delete","params":{{"record_ids":"all"}}}}
update:  {{"intent":"update","params":{{"record_id":"<id>","new_file_path":"<path>"}}}}
query:   {{"intent":"query","params":{{"question":"{user_input}"}}}}

USER MESSAGE: {user_input}
"""


# ─────────────────────────────────────────────────────────────
# DOCUMENT STRUCTURER PROMPT
# ─────────────────────────────────────────────────────────────

def structurer_prompt(text_content: str) -> str:
    return f"""You are an intelligent document parser.
Extract ALL meaningful structured information from the document.
Return ONLY valid JSON — no markdown, no explanation.

Rules:
- Do not use a fixed schema; create dynamic keys that reflect actual content.
- Preserve hierarchy. Extract entities, metadata, tables, lists, key-value pairs.

DOCUMENT:
{text_content[:12000]}
"""


# ─────────────────────────────────────────────────────────────
# Q&A PROMPT
# ─────────────────────────────────────────────────────────────

def requirement_qa_prompt(documents: dict, doc_context: str) -> str:
    doc_names = list(documents.keys())
    names_block = "\n".join(f"  - {n}" for n in doc_names)
    return f"""You are a Requirements Analyst assistant with expertise in procurement.

You have access to {len(documents)} requirement document(s):
{names_block}

Answer any question about requirement content: specs, quantities, budgets,
departments, deadlines, categories, cross-document comparisons.

GUIDELINES:
- Always cite which document your answer comes from.
- For counting and aggregation, scan all documents and sum or compare.
- For filtering, return only matching entries across all documents.
- Present answers in a clean, structured format.
- If the answer is not in any document, say so — do not guess.

BOUNDARY RULE:
  If the user asks you to find vendors, match vendors, score vendors, or
  recommend suppliers for a requirement, respond with EXACTLY this and nothing else:
  "BOUNDARY: Vendor matching and scoring is handled by Jarvis at the root level."

--- DOCUMENTS CONTEXT ---
{doc_context}
"""
