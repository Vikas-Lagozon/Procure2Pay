# quotation_agent/prompts.py
# All prompt templates for the Quotation Agent.

from __future__ import annotations


# ─────────────────────────────────────────────────────────────
# HELP TEXT
# ─────────────────────────────────────────────────────────────

HELP_TEXT: str = """
Quotation Chatbot — Natural Language Interface

Just talk naturally — no commands required.

UPLOAD    "Upload docs/dell_quotation_q3.pdf"
LIST      "Show all quotations"
VIEW      "Show details of the Dell quotation"  /  "Get record <id>"
DELETE    "Delete the Dell quotation"  /  "Remove all quotations"
UPDATE    "Update the Dell quotation with dell_q4.pdf"
QUERY     "Which quotations are valid this month?"
          "What is the total price in the Infosys quotation?"
          "Compare delivery lead times across all quotations"
          "Which vendor gave the lowest price for laptops?"

Type /help_quotation to see this again, /exit to quit.
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

    return f"""You are an intent detection engine for a Quotation Management chatbot.
Return ONE JSON object — nothing else.

QUOTATION DOCUMENTS IN SESSION:
{doc_lines.strip() if doc_lines.strip() else "  (none — DB will be queried at runtime)"}

INTENTS: upload | list | get | delete | update | query

RULES:
1. Extract file path from any token ending in .docx / .pdf / .txt / .md
2. "all", "both", "everything", "all of them" -> record_ids = "all"
3. Quotation references like "the Dell one" -> resolve to record_id from session list above
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
# STRUCTURING PROMPT
# ─────────────────────────────────────────────────────────────

def structuring_prompt(document_text: str) -> str:
    return f"""You are a document parser specialised in vendor quotation documents.
Extract ALL meaningful structured information. Return ONLY valid JSON — no markdown, no explanation.

Always extract these fields if present:
  quotation_number, quotation_date, validity_date, vendor_name, vendor_address,
  contact_person, contact_email, contact_phone,
  buyer_name, buyer_address,
  line_items (list of: item_description, quantity, unit, unit_price, total_price, sku/part_number),
  subtotal, taxes, discounts, grand_total, currency,
  payment_terms, delivery_lead_time, delivery_terms, warranty,
  certifications, special_conditions, notes

Also extract any other fields with commercial or logistical value.
Use dynamic keys that reflect actual content. Preserve hierarchy.

DOCUMENT:
{document_text[:12000]}
"""


# ─────────────────────────────────────────────────────────────
# QUOTATION Q&A PROMPT
# ─────────────────────────────────────────────────────────────

def quotation_qa_prompt(
    num_documents: int,
    doc_names: list[str],
    doc_context: str,
) -> str:
    names_block = "\n".join(f"  - {n}" for n in doc_names)
    return f"""You are a Quotation Management assistant with expertise in procurement and sourcing.

You have access to {num_documents} quotation document(s):
{names_block}

Answer any question about quotation content: pricing, line items, validity dates,
vendor details, payment and delivery terms, taxes, discounts, comparisons across quotations.

GUIDELINES:
- Always cite which document your answer comes from.
- For pricing questions, return: item name, unit price, quantity, total, and currency.
- For validity questions, check the validity_date field and compare with today's date.
- For filtering, scan all documents and return only matching entries.
- For aggregation (totals, averages, best price), scan all documents and compute as needed.
- For comparison questions, present a structured table or side-by-side view.
- If the answer is not in any document, say so — do not guess.
- Present answers in a clean, structured format.

BOUNDARY RULE:
  If the user asks you to score, rank, or select the best quotation based on weighted
  criteria or a requirements document, respond with EXACTLY this and nothing else:
  "BOUNDARY: Quotation scoring and selection is handled by Jarvis at the root level."

--- QUOTATION DOCUMENTS CONTEXT ---
{doc_context}
"""


# ─────────────────────────────────────────────────────────────
# SUCCESS MESSAGES
# ─────────────────────────────────────────────────────────────

def upload_success_message(
    original_name: str,
    inserted_id: str,
    text_length: int,
    num_docs_in_session: int,
    doc_list: str,
) -> str:
    return (
        f"Quotation document uploaded successfully.\n\n"
        f"File     : {original_name}\n"
        f"Record ID: {inserted_id}\n"
        f"Length   : {text_length} characters\n\n"
        f"Quotation documents in session ({num_docs_in_session}):\n"
        f"{doc_list}\n\n"
        f"You can now ask questions about any of these quotation documents."
    )


def update_success_message(
    old_file_name: str,
    file_msg: str,
    db_msg: str,
    session_msg: str,
    new_text_length: int,
    structured_keys: list[str],
) -> str:
    return (
        f"Update complete for: {old_file_name}\n\n"
        f"{file_msg}\n"
        f"{db_msg}\n"
        f"{session_msg}\n\n"
        f"New content length : {new_text_length} characters\n"
        f"Structured keys    : {structured_keys}\n\n"
        f"You can now ask questions about the updated quotation document."
    )


def delete_success_message(
    file_name: str,
    db_msg: str,
    file_msg: str,
    session_msg: str,
) -> str:
    return (
        f"Delete complete for: {file_name}\n\n"
        f"{db_msg}\n"
        f"{file_msg}\n"
        f"{session_msg}"
    )

