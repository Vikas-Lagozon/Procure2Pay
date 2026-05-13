# root_agent/tools.py
# ─────────────────────────────────────────────────────────────
# Direct-DB tools for Jarvis (root agent).
#
# WHY these tools exist
# ─────────────────────
# Google ADK's transfer_to_agent is a full round-trip LLM call.
# Using it just to *read* data from MongoDB is wasteful and slow.
# These tools hit MongoDB directly, format the result, and return
# a plain string that Jarvis can present immediately — no sub-agent
# hop required for READ operations.
#
# CRUD operations (upload / delete / update) still go to sub-agents
# because those agents own the file-system logic and session state.
#
# TOOL GROUPS
# ───────────
#  Requirements   : list_requirements, get_requirement,
#                   count_requirements_by_category
#  Vendors        : list_vendors, get_vendor, get_vendor_contact_info
#  Quotations     : list_quotations, get_quotation, get_quotations_by_vendor
#  Scoring        : score_vendors_for_requirement, generate_full_score_matrix,
#                   check_quotation_coverage, compare_quotations_for_requirement,
#                   rank_vendors_overall, score_vendor_across_requirements,
#                   compare_vendors_head_to_head, find_best_vendor_for_category
#  Matching       : find_requirements_for_vendor
#  Cross-collection: get_requirement_with_vendor_context, search_across_all,
#                    get_vendor_full_profile, get_procurement_summary,
#                    find_unquoted_requirements, get_rfq_readiness_report
# ─────────────────────────────────────────────────────────────

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# ── Ensure root_agent/ is on sys.path ───────────────────────
_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
# ─────────────────────────────────────────────────────────────

from logger import get_logger
from utils import (
    check_quotation_requirement_coverage,
    extract_field,
    format_best_vendor_for_category,
    format_head_to_head,
    format_overall_vendor_ranking,
    format_procurement_summary,
    format_quotation_comparison,
    format_quotation_coverage_report,
    format_rfq_readiness,
    format_score_matrix,
    format_unquoted_requirements,
    format_vendor_across_requirements,
    format_vendor_full_profile,
    format_vendor_score_report,
    get_all_text,
    get_doc_display_name,
    get_vendor_name,
    keyword_in_doc,
    normalize_text,
    score_vendor_against_requirement,
    _detect_categories,
)

from config import config

logger = get_logger(__name__)

# ─────────────────────────────────────────────────────────────
# LAZY MONGO COLLECTION FACTORY
# ─────────────────────────────────────────────────────────────

def _col(name: str):
    """Return a MongoCollection instance for *name*."""
    try:
        from requirements_agent.nosql_db import MongoCollection
    except ImportError:
        try:
            from vendors_agent.nosql_db import MongoCollection  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "Cannot import MongoCollection — ensure at least one sub-agent "
                f"package is on sys.path. Original error: {exc}"
            ) from exc
    return MongoCollection(name)


def _req_col():
    return _col(config.REQUIREMENTS_COLLECTION)

def _vendor_col():
    return _col(config.VENDORS_COLLECTION)

def _quotation_col():
    return _col(config.QUOTATIONS_COLLECTION)


# ─────────────────────────────────────────────────────────────
# INTERNAL HELPERS
# ─────────────────────────────────────────────────────────────

def _safe_fetch_all(col_fn, context: str) -> tuple[list[dict], str]:
    """Fetch all docs from a collection; return (docs, error_msg)."""
    try:
        docs = col_fn().fetch_all()
        return docs, ""
    except Exception as exc:
        logger.error(f"[tools] {context} DB error: {exc}")
        return [], f"Database error while fetching {context}: {exc}"


def _find_docs_by_keyword(
    docs: list[dict],
    keyword: str,
    label: str,
    list_fn_name: str,
) -> tuple[list[dict], str]:
    """Filter *docs* by *keyword*. Returns (matches, error_or_empty_msg)."""
    matches = [d for d in docs if keyword_in_doc(keyword, d)]
    if not matches:
        names = ", ".join(f"'{get_doc_display_name(d)}'" for d in docs[:8])
        suffix = f"  Available: {names}" if names else ""
        return [], f"No {label} found matching '{keyword}'.{suffix}"
    return matches, ""


# ─────────────────────────────────────────────────────────────
# ── REQUIREMENT TOOLS ────────────────────────────────────────
# ─────────────────────────────────────────────────────────────

def list_requirements(limit: int = 100) -> str:
    """
    List ALL requirement documents stored in the database.

    Returns a formatted summary (name, department, quantity, budget,
    category, record ID) for every requirement.

    Use when the user asks:
      "show all requirements", "list requirements",
      "how many requirements do we have", "give me the requirements"
    """
    docs, err = _safe_fetch_all(_req_col, "requirements")
    if err:
        return err
    if not docs:
        return "No requirements found in the database."

    lines = [f"REQUIREMENTS — {len(docs)} document(s)", "=" * 54]
    for i, doc in enumerate(docs, 1):
        sd   = doc.get("structured_data") or {}
        name = get_doc_display_name(doc)
        dept = extract_field(sd, "department", "dept", "division", default="N/A")
        qty  = extract_field(sd, "quantity", "total_quantity", "units", "nos", default="N/A")
        bgt  = extract_field(sd, "budget", "total_budget", "budget_amount",
                             "estimated_budget", default="N/A")
        cats = extract_field(sd, "categories", "category", "item_type",
                             "product_category", default="N/A")
        if isinstance(cats, list):
            cats = ", ".join(str(c) for c in cats[:5])
        rid  = doc.get("record_id", str(doc.get("_id", "")))

        lines += [
            f"\n  [{i}] {name}",
            f"       File       : {doc.get('file_name', 'N/A')}",
            f"       Department : {dept}",
            f"       Quantity   : {qty}",
            f"       Budget     : {bgt}",
            f"       Category   : {cats}",
            f"       Record ID  : {rid}",
        ]

    return "\n".join(lines)


def get_requirement(keyword: str) -> str:
    """
    Get the full structured details of a requirement by keyword search.

    *keyword* can be a name, department, category, or any term that
    appears in the requirement document.

    Use when the user asks:
      "show details of laptop requirement",
      "what are the specs in the printer requirement",
      "tell me about the IT requirement"
    """
    docs, err = _safe_fetch_all(_req_col, "requirements")
    if err:
        return err

    matches, msg = _find_docs_by_keyword(docs, keyword, "requirement",
                                          "list_requirements()")
    if not matches:
        return msg

    lines = [f"REQUIREMENT DETAILS — '{keyword}'", "=" * 54]
    for doc in matches:
        sd   = doc.get("structured_data") or {}
        name = get_doc_display_name(doc)
        rid  = doc.get("record_id", str(doc.get("_id", "")))

        lines += [
            f"\n{'─'*54}",
            f"Name      : {name}",
            f"File      : {doc.get('file_name', 'N/A')}",
            f"Record ID : {rid}",
            f"File Path : {doc.get('file_path', 'N/A')}",
            "\nStructured Data:",
        ]
        if sd:
            lines.append(json.dumps(sd, indent=2, ensure_ascii=False)[:4000])
        else:
            lines.append("  (structured data not available)")

        preview = (doc.get("text_content") or "")[:600]
        if preview:
            lines += ["\nText Preview:", preview + ("…" if len(doc.get("text_content","")) > 600 else "")]

    return "\n".join(lines)


def count_requirements_by_category(category_keyword: str = "") -> str:
    """
    Count requirements, optionally filtered by a category keyword.

    *category_keyword* examples: "technical", "IT", "non-technical", "laptop"
    Leave blank to count all requirements.

    Use when the user asks:
      "how many technical requirements do we have",
      "count of IT requirements", "total requirements"
    """
    docs, err = _safe_fetch_all(_req_col, "requirements")
    if err:
        return err
    if not docs:
        return "No requirements found in the database."

    if not category_keyword:
        return f"Total requirements in the database: {len(docs)}"

    matches = [d for d in docs if keyword_in_doc(category_keyword, d)]
    names   = "\n".join(f"  • {get_doc_display_name(d)}" for d in matches)
    return (
        f"Requirements matching '{category_keyword}': {len(matches)} of {len(docs)}\n\n"
        + (names if names else "  (none)")
    )


# ─────────────────────────────────────────────────────────────
# ── VENDOR TOOLS ─────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────

def list_vendors(limit: int = 100) -> str:
    """
    List ALL vendor documents stored in the database with contact info.

    Use when the user asks:
      "show all vendors", "list vendors",
      "what vendors do we have", "how many vendors"
    """
    docs, err = _safe_fetch_all(_vendor_col, "vendors")
    if err:
        return err
    if not docs:
        return "No vendors found in the database."

    lines = [f"VENDORS — {len(docs)} document(s)", "=" * 54]
    for i, doc in enumerate(docs, 1):
        sd      = doc.get("structured_data") or {}
        vname   = get_vendor_name(doc)
        person  = extract_field(sd, "contact_person", "contact_name", "person",
                                "representative", default="N/A")
        email   = extract_field(sd, "contact_email", "email", "email_address",
                                "mail", default="N/A")
        phone   = extract_field(sd, "contact_phone", "phone", "mobile",
                                "telephone", default="N/A")
        cats    = extract_field(sd, "product_categories", "categories",
                                "products", "product_range", default="N/A")
        if isinstance(cats, list):
            cats = ", ".join(str(c) for c in cats[:5])
        certs   = extract_field(sd, "certifications", "certificates", default="N/A")
        if isinstance(certs, list):
            certs = ", ".join(str(c) for c in certs[:4])
        rid     = doc.get("record_id", str(doc.get("_id", "")))

        lines += [
            f"\n  [{i}] {vname}",
            f"       Contact    : {person}",
            f"       Email      : {email}",
            f"       Phone      : {phone}",
            f"       Categories : {cats}",
            f"       Certs      : {certs}",
            f"       Record ID  : {rid}",
        ]

    return "\n".join(lines)


def get_vendor(keyword: str) -> str:
    """
    Get full details of a vendor by keyword search.

    Use when the user asks:
      "show details of Dell vendor",
      "what products does HP India offer",
      "tell me about Bansal Technology"
    """
    docs, err = _safe_fetch_all(_vendor_col, "vendors")
    if err:
        return err

    matches, msg = _find_docs_by_keyword(docs, keyword, "vendor", "list_vendors()")
    if not matches:
        return msg

    lines = [f"VENDOR DETAILS — '{keyword}'", "=" * 54]
    for doc in matches:
        sd    = doc.get("structured_data") or {}
        vname = get_vendor_name(doc)
        rid   = doc.get("record_id", str(doc.get("_id", "")))

        lines += [
            f"\n{'─'*54}",
            f"Vendor    : {vname}",
            f"File      : {doc.get('file_name', 'N/A')}",
            f"Record ID : {rid}",
            "\nStructured Data:",
        ]
        if sd:
            lines.append(json.dumps(sd, indent=2, ensure_ascii=False)[:4000])
        else:
            lines.append("  (structured data not available)")

    return "\n".join(lines)


def get_vendor_contact_info(vendor_keyword: str) -> str:
    """
    Get contact details (email, phone, contact person, company address)
    for a vendor by keyword search.

    This is the REQUIRED first step before sending any email to a vendor.
    Never pass vendor names to email tools — always resolve email via this tool first.

    Use when the user asks:
      "what is the email of Code Lab",
      "contact info for Bansal Technology",
      "send email to Dell vendor" (resolve email first)
    """
    docs, err = _safe_fetch_all(_vendor_col, "vendors")
    if err:
        return err

    matches, msg = _find_docs_by_keyword(docs, vendor_keyword, "vendor", "list_vendors()")
    if not matches:
        return msg

    lines = [f"VENDOR CONTACT INFO — '{vendor_keyword}'", "=" * 54]
    for doc in matches:
        sd      = doc.get("structured_data") or {}
        vname   = get_vendor_name(doc)
        company = extract_field(sd, "company_name", "vendor_name",
                                "organization", default=vname)
        person  = extract_field(sd, "contact_person", "contact_name",
                                "person", "representative", default="Not specified")
        email   = extract_field(sd, "contact_email", "email", "email_address",
                                "mail", "e-mail", default="NOT FOUND")
        phone   = extract_field(sd, "contact_phone", "phone", "mobile",
                                "telephone", "contact_number", default="Not specified")
        address = extract_field(sd, "company_address", "address",
                                "location", "office_address", default="Not specified")
        website = extract_field(sd, "website", "web", "url", default="N/A")

        lines += [
            f"\n  Vendor       : {vname}",
            f"  Company      : {company}",
            f"  Contact      : {person}",
            f"  Email        : {email}",
            f"  Phone        : {phone}",
            f"  Address      : {address}",
            f"  Website      : {website}",
        ]

    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────
# ── QUOTATION TOOLS ──────────────────────────────────────────
# ─────────────────────────────────────────────────────────────

def list_quotations(limit: int = 100) -> str:
    """
    List ALL quotation documents with vendor mapping (email cross-referenced
    from the vendors collection).

    Use when the user asks:
      "show all quotations", "list quotations",
      "what quotations do we have", "show quotations with vendor info"
    """
    q_docs, err = _safe_fetch_all(_quotation_col, "quotations")
    if err:
        return err
    if not q_docs:
        return "No quotations found in the database."

    # Build vendor email map for cross-referencing
    v_docs, _ = _safe_fetch_all(_vendor_col, "vendors")
    vendor_email_map: dict[str, dict] = {}
    for v in v_docs:
        sd    = v.get("structured_data") or {}
        vname = normalize_text(get_vendor_name(v))
        vendor_email_map[vname] = {
            "email":   extract_field(sd, "contact_email", "email", default="N/A"),
            "contact": extract_field(sd, "contact_person", "contact_name", default="N/A"),
        }

    lines = [f"QUOTATIONS — {len(q_docs)} document(s)  (vendor emails cross-referenced)", "=" * 62]
    for i, q in enumerate(q_docs, 1):
        sd       = q.get("structured_data") or {}
        q_name   = get_doc_display_name(q)
        vname    = get_vendor_name(q)
        q_num    = extract_field(sd, "quotation_number", "quote_no",
                                 "quote_number", "ref_no", "reference", default="N/A")
        q_date   = extract_field(sd, "quotation_date", "date",
                                 "issue_date", "prepared_date", default="N/A")
        validity = extract_field(sd, "validity_date", "valid_until",
                                 "expiry_date", "expiry", default="N/A")
        total    = extract_field(sd, "grand_total", "total_amount",
                                 "total", "amount", "net_total", default="N/A")
        currency = extract_field(sd, "currency", "curr", default="INR")
        items    = extract_field(sd, "line_items", "items", "products", default=[])
        n_items  = len(items) if isinstance(items, list) else "N/A"
        rid      = q.get("record_id", str(q.get("_id", "")))

        # Cross-reference vendor email
        v_info  = vendor_email_map.get(normalize_text(vname), {})
        v_email = v_info.get("email", "Not in vendor database")
        v_cont  = v_info.get("contact", "N/A")

        lines += [
            f"\n  [{i}] {q_name}",
            f"       Quotation # : {q_num}",
            f"       Vendor      : {vname}",
            f"       Vnd. Contact: {v_cont}  |  Email: {v_email}",
            f"       Date        : {q_date}",
            f"       Valid Until : {validity}",
            f"       Grand Total : {currency} {total}",
            f"       Line Items  : {n_items}",
            f"       Record ID   : {rid}",
        ]

    return "\n".join(lines)


def get_quotation(keyword: str) -> str:
    """
    Get full details of a quotation by keyword search (vendor name,
    quotation number, or any relevant term).

    Use when the user asks:
      "show details of the Dell quotation",
      "get quotation Q/2024/001",
      "what are the line items in the Code Lab quotation"
    """
    docs, err = _safe_fetch_all(_quotation_col, "quotations")
    if err:
        return err

    matches, msg = _find_docs_by_keyword(docs, keyword, "quotation", "list_quotations()")
    if not matches:
        return msg

    lines = [f"QUOTATION DETAILS — '{keyword}'", "=" * 54]
    for doc in matches:
        sd    = doc.get("structured_data") or {}
        qname = get_doc_display_name(doc)
        vname = get_vendor_name(doc)
        rid   = doc.get("record_id", str(doc.get("_id", "")))

        lines += [
            f"\n{'─'*54}",
            f"Quotation : {qname}",
            f"Vendor    : {vname}",
            f"File      : {doc.get('file_name', 'N/A')}",
            f"Record ID : {rid}",
            "\nStructured Data:",
        ]
        if sd:
            lines.append(json.dumps(sd, indent=2, ensure_ascii=False)[:5000])
        else:
            lines.append("  (structured data not available)")

        preview = (doc.get("text_content") or "")[:500]
        if preview:
            lines += ["\nText Preview:", preview + "…"]

    return "\n".join(lines)


def get_quotations_by_vendor(vendor_keyword: str) -> str:
    """
    Retrieve all quotations that belong to (or mention) a specific vendor.

    Use when the user asks:
      "how many quotations do we have from Code Lab",
      "show all quotations from Dell",
      "list quotations from Bansal Technology"
    """
    docs, err = _safe_fetch_all(_quotation_col, "quotations")
    if err:
        return err

    matches, msg = _find_docs_by_keyword(docs, vendor_keyword, "quotation",
                                          "list_quotations()")
    if not matches:
        return msg

    lines = [
        f"QUOTATIONS FROM VENDOR: '{vendor_keyword}'  ({len(matches)} found)",
        "=" * 62,
    ]
    for i, doc in enumerate(matches, 1):
        sd       = doc.get("structured_data") or {}
        vname    = get_vendor_name(doc)
        q_num    = extract_field(sd, "quotation_number", "quote_no", default="N/A")
        q_date   = extract_field(sd, "quotation_date", "date", default="N/A")
        validity = extract_field(sd, "validity_date", "valid_until", default="N/A")
        total    = extract_field(sd, "grand_total", "total_amount", "total", default="N/A")
        currency = extract_field(sd, "currency", default="INR")
        rid      = doc.get("record_id", str(doc.get("_id", "")))
        items    = extract_field(sd, "line_items", "items", "products", default=[])
        n_items  = len(items) if isinstance(items, list) else "N/A"

        lines += [
            f"\n  [{i}] {doc.get('file_name', 'Quotation')}",
            f"       Vendor       : {vname}",
            f"       Quotation #  : {q_num}",
            f"       Date         : {q_date}",
            f"       Valid Until  : {validity}",
            f"       Grand Total  : {currency} {total}",
            f"       Line Items   : {n_items}",
            f"       Record ID    : {rid}",
        ]

    lines += [
        "",
        f"Total quotations from '{vendor_keyword}': {len(matches)}",
    ]
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────
# ── SCORING / MATCHING TOOLS ─────────────────────────────────
# ─────────────────────────────────────────────────────────────

def score_vendors_for_requirement(
    requirement_keyword: str,
    top_n: int = 0,
) -> str:
    """
    Score and rank ALL vendors against a specific requirement using
    5-dimension analysis (Category 30 + Spec 30 + Budget 20 + Cert 10 + Delivery 10 = 100%).

    *top_n* limits the number of vendors shown (0 = show all).

    Use when the user asks:
      "find top 3 vendors for laptop requirement",
      "which vendor is best for the printer requirement",
      "rank vendors against the IT requirement with scores"
    """
    req_docs, err = _safe_fetch_all(_req_col, "requirements")
    if err:
        return err
    v_docs, err = _safe_fetch_all(_vendor_col, "vendors")
    if err:
        return err

    if not req_docs:
        return "No requirements found in the database."
    if not v_docs:
        return "No vendors found in the database."

    req_matches, msg = _find_docs_by_keyword(req_docs, requirement_keyword,
                                              "requirement", "list_requirements()")
    if not req_matches:
        return msg

    req_doc  = req_matches[0]
    req_name = get_doc_display_name(req_doc)

    logger.info(f"[tools] Scoring {len(v_docs)} vendor(s) against '{req_name}'")
    scored = [score_vendor_against_requirement(req_doc, v) for v in v_docs]

    n = top_n if top_n > 0 else None
    return format_vendor_score_report(scored, req_name, n)


def generate_full_score_matrix() -> str:
    """
    Generate a complete N×M score matrix: every requirement scored
    against every vendor.

    Use when the user asks:
      "create a score table for all requirements and vendors",
      "show me the full vendor scoring matrix",
      "which vendor is best overall across all our requirements"
    """
    req_docs, err = _safe_fetch_all(_req_col, "requirements")
    if err:
        return err
    v_docs, err = _safe_fetch_all(_vendor_col, "vendors")
    if err:
        return err

    if not req_docs:
        return "No requirements found in the database."
    if not v_docs:
        return "No vendors found in the database."

    logger.info(
        f"[tools] Building score matrix: {len(req_docs)} req(s) × {len(v_docs)} vendor(s)"
    )

    matrix = []
    for req in req_docs:
        scored_vendors = [score_vendor_against_requirement(req, v) for v in v_docs]
        matrix.append({
            "requirement_name": get_doc_display_name(req),
            "requirement_id":   str(req.get("record_id", req.get("_id", ""))),
            "vendors":          scored_vendors,
        })

    return format_score_matrix(matrix)


def check_quotation_coverage(quotation_keyword: str) -> str:
    """
    Check how many (and which) requirements a specific quotation can
    potentially fulfill based on category and item overlap.

    Use when the user asks:
      "how many requirements can the Chitkara Lab quotation fulfill",
      "which requirements does the Dell quotation cover",
      "can the HP quotation satisfy our needs"
    """
    q_docs,   err = _safe_fetch_all(_quotation_col, "quotations")
    if err:
        return err
    req_docs, err = _safe_fetch_all(_req_col, "requirements")
    if err:
        return err

    if not q_docs:
        return "No quotations found in the database."
    if not req_docs:
        return "No requirements found in the database."

    q_matches, msg = _find_docs_by_keyword(q_docs, quotation_keyword,
                                            "quotation", "list_quotations()")
    if not q_matches:
        return msg

    q_doc   = q_matches[0]
    q_label = f"{get_doc_display_name(q_doc)} (by {get_vendor_name(q_doc)})"

    logger.info(f"[tools] Checking quotation coverage: '{q_label}'")
    results = check_quotation_requirement_coverage(q_doc, req_docs)
    return format_quotation_coverage_report(q_label, results)


def compare_quotations_for_requirement(requirement_keyword: str) -> str:
    """
    Compare ALL quotations received against a specific requirement.
    Scores each quotation on the same 5-dimension framework and
    produces a ranked comparison table.

    Use when the user asks:
      "compare quotations for the laptop requirement",
      "which quotation is best for printer procurement",
      "evaluate all quotes against the server requirement"
    """
    req_docs, err = _safe_fetch_all(_req_col, "requirements")
    if err:
        return err
    q_docs, err = _safe_fetch_all(_quotation_col, "quotations")
    if err:
        return err

    if not req_docs:
        return "No requirements found in the database."
    if not q_docs:
        return "No quotations found in the database."

    req_matches, msg = _find_docs_by_keyword(req_docs, requirement_keyword,
                                              "requirement", "list_requirements()")
    if not req_matches:
        return msg

    req_doc  = req_matches[0]
    req_name = get_doc_display_name(req_doc)

    rows = []
    for q_doc in q_docs:
        sd       = q_doc.get("structured_data") or {}
        total    = extract_field(sd, "grand_total", "total_amount", "total", default="N/A")
        currency = extract_field(sd, "currency", default="INR")
        validity = extract_field(sd, "validity_date", "valid_until", default="N/A")
        payment  = extract_field(sd, "payment_terms", "payment", default="N/A")
        delivery = extract_field(sd, "delivery_lead_time", "delivery",
                                 "lead_time", "delivery_terms", default="N/A")

        rows.append({
            "quotation_name": get_doc_display_name(q_doc),
            "vendor_name":    get_vendor_name(q_doc),
            "grand_total":    f"{currency} {total}",
            "validity":       validity,
            "payment_terms":  payment,
            "delivery":       delivery,
            "score":          score_vendor_against_requirement(req_doc, q_doc),
        })

    return format_quotation_comparison(req_name, rows)


def rank_vendors_overall(top_n: int = 0) -> str:
    """
    Rank ALL vendors by their AGGREGATE score across every requirement.

    Computes average score, max, min, and how many requirements each
    vendor is relevant for (score ≥ 50%) — giving a clear picture of
    which vendor is the most capable partner overall.

    *top_n* limits the output (0 = show all).

    Use when the user asks:
      "which vendor is the best overall",
      "rank all vendors across all our requirements",
      "who is our strongest vendor partner",
      "give me a vendor leaderboard"
    """
    req_docs, err = _safe_fetch_all(_req_col, "requirements")
    if err:
        return err
    v_docs, err = _safe_fetch_all(_vendor_col, "vendors")
    if err:
        return err

    if not req_docs:
        return "No requirements found in the database."
    if not v_docs:
        return "No vendors found in the database."

    logger.info(
        f"[tools] Computing aggregate ranking: {len(v_docs)} vendor(s) "
        f"× {len(req_docs)} requirement(s)"
    )

    ranked = []
    for v_doc in v_docs:
        scores_per_req = []
        for req_doc in req_docs:
            result = score_vendor_against_requirement(req_doc, v_doc)
            scores_per_req.append({
                "req_name": get_doc_display_name(req_doc),
                "score":    result["total_score"],
            })

        all_scores = [s["score"] for s in scores_per_req]
        avg_score  = round(sum(all_scores) / len(all_scores)) if all_scores else 0

        ranked.append({
            "vendor_name":   get_vendor_name(v_doc),
            "avg_score":     avg_score,
            "max_score":     max(all_scores) if all_scores else 0,
            "min_score":     min(all_scores) if all_scores else 0,
            "n_requirements": len(req_docs),
            "n_relevant":    sum(1 for s in all_scores if s >= 50),
            "per_req":       scores_per_req,
        })

    ranked.sort(key=lambda x: (x["avg_score"], x["max_score"]), reverse=True)
    n = top_n if top_n > 0 else None
    return format_overall_vendor_ranking(ranked, n)


def score_vendor_across_requirements(vendor_keyword: str) -> str:
    """
    Score ONE specific vendor against ALL requirements and show a
    per-requirement breakdown — a reverse view of score_vendors_for_requirement.

    Use when the user asks:
      "how good is Bansal Technology for all our requirements",
      "score Code Lab against every requirement we have",
      "show me all requirement scores for Chitkara vendor"
    """
    v_docs, err = _safe_fetch_all(_vendor_col, "vendors")
    if err:
        return err
    req_docs, err = _safe_fetch_all(_req_col, "requirements")
    if err:
        return err

    if not v_docs:
        return "No vendors found in the database."
    if not req_docs:
        return "No requirements found in the database."

    v_matches, msg = _find_docs_by_keyword(v_docs, vendor_keyword, "vendor", "list_vendors()")
    if not v_matches:
        return msg

    v_doc      = v_matches[0]
    vendor_name = get_vendor_name(v_doc)

    logger.info(f"[tools] Scoring '{vendor_name}' across {len(req_docs)} requirement(s)")

    results = []
    for req_doc in req_docs:
        res = score_vendor_against_requirement(req_doc, v_doc)
        results.append({
            "req_name":   get_doc_display_name(req_doc),
            "score":      res["total_score"],
            "dimensions": res["dimensions"],
            "gaps":       res["gaps"],
        })

    results.sort(key=lambda x: x["score"], reverse=True)
    return format_vendor_across_requirements(vendor_name, results)


def compare_vendors_head_to_head(
    vendor1_keyword: str,
    vendor2_keyword: str,
    requirement_keyword: str = "",
) -> str:
    """
    Compare TWO vendors directly against each other — dimension by dimension.

    If *requirement_keyword* is provided, comparison is scoped to that
    requirement only. Otherwise both vendors are compared across ALL
    requirements simultaneously.

    Use when the user asks:
      "compare Bansal Technology vs Code Lab",
      "head to head: Chitkara vs Bansal for laptop requirement",
      "which is better between Dell and HP",
      "side by side comparison of two vendors"
    """
    v_docs, err = _safe_fetch_all(_vendor_col, "vendors")
    if err:
        return err
    req_docs, err = _safe_fetch_all(_req_col, "requirements")
    if err:
        return err

    if not v_docs:
        return "No vendors found in the database."
    if not req_docs:
        return "No requirements found in the database."

    v1_matches, msg1 = _find_docs_by_keyword(v_docs, vendor1_keyword, "vendor", "list_vendors()")
    if not v1_matches:
        return msg1

    v2_matches, msg2 = _find_docs_by_keyword(v_docs, vendor2_keyword, "vendor", "list_vendors()")
    if not v2_matches:
        return msg2

    v1_doc = v1_matches[0]
    v2_doc = v2_matches[0]
    v1_name = get_vendor_name(v1_doc)
    v2_name = get_vendor_name(v2_doc)

    # Filter requirements if a keyword was given
    if requirement_keyword.strip():
        scope_reqs, msg = _find_docs_by_keyword(
            req_docs, requirement_keyword, "requirement", "list_requirements()"
        )
        if not scope_reqs:
            return msg
        scope_label = f"Requirement: {get_doc_display_name(scope_reqs[0])}"
    else:
        scope_reqs  = req_docs
        scope_label = "All Requirements"

    logger.info(
        f"[tools] Head-to-head: '{v1_name}' vs '{v2_name}' | scope: {scope_label}"
    )

    comparisons = []
    for req_doc in scope_reqs:
        r1 = score_vendor_against_requirement(req_doc, v1_doc)
        r2 = score_vendor_against_requirement(req_doc, v2_doc)

        s1, s2 = r1["total_score"], r2["total_score"]
        winner = "v1" if s1 > s2 else ("v2" if s2 > s1 else "tie")

        comparisons.append({
            "req_name": get_doc_display_name(req_doc),
            "v1_score": s1,
            "v2_score": s2,
            "winner":   winner,
            "v1_dims":  r1["dimensions"],
            "v2_dims":  r2["dimensions"],
        })

    return format_head_to_head(v1_name, v2_name, scope_label, comparisons)


def find_best_vendor_for_category(category_keyword: str) -> str:
    """
    Find which vendors are best suited for a specific product category
    (e.g. "laptop", "networking", "printer", "gpu").

    Scores all vendors that have any relevance to the given category
    and ranks them — useful for category-driven procurement decisions.

    Use when the user asks:
      "who is the best vendor for laptops",
      "which vendor should we approach for networking equipment",
      "find top GPU vendors",
      "best vendor for printing category"
    """
    v_docs, err = _safe_fetch_all(_vendor_col, "vendors")
    if err:
        return err
    req_docs, _ = _safe_fetch_all(_req_col, "requirements")

    if not v_docs:
        return "No vendors found in the database."

    # Filter requirements to only those matching the category
    cat_reqs = [r for r in req_docs if keyword_in_doc(category_keyword, r)]
    if not cat_reqs:
        cat_reqs = req_docs  # fall back to all if no category-specific reqs

    logger.info(
        f"[tools] Category best-vendor: '{category_keyword}' | "
        f"{len(cat_reqs)} relevant requirement(s)"
    )

    results = []
    for v_doc in v_docs:
        v_text = get_all_text(v_doc)
        v_cats = _detect_categories(v_text)
        sd     = v_doc.get("structured_data") or {}

        # Score vendor against category-relevant requirements
        scores = []
        for req_doc in cat_reqs:
            res = score_vendor_against_requirement(req_doc, v_doc)
            scores.append(res["total_score"])

        avg_score = round(sum(scores) / len(scores)) if scores else 0
        n_matched = sum(1 for s in scores if s >= 50)

        certs = extract_field(sd, "certifications", "certificates", default="N/A")
        if isinstance(certs, list):
            certs = ", ".join(str(c) for c in certs[:3])
        email = extract_field(sd, "contact_email", "email", default="N/A")

        results.append({
            "vendor_name":  get_vendor_name(v_doc),
            "score":        avg_score,
            "matched_reqs": f"{n_matched}/{len(cat_reqs)}",
            "email":        email,
            "certs":        certs,
        })

    results.sort(key=lambda x: x["score"], reverse=True)
    # Only show vendors with some relevance
    relevant = [r for r in results if r["score"] > 0] or results
    return format_best_vendor_for_category(category_keyword, relevant)


def find_requirements_for_vendor(vendor_keyword: str) -> str:
    """
    Reverse lookup: find which requirements a specific vendor is BEST
    suited for — ranked from highest to lowest match score.

    Use when the user asks:
      "what requirements can Bansal Technology fulfill",
      "which of our needs does Code Lab cover best",
      "show me the best requirements for Chitkara vendor",
      "where does this vendor fit in our procurement"
    """
    v_docs, err = _safe_fetch_all(_vendor_col, "vendors")
    if err:
        return err
    req_docs, err = _safe_fetch_all(_req_col, "requirements")
    if err:
        return err

    if not v_docs:
        return "No vendors found in the database."
    if not req_docs:
        return "No requirements found in the database."

    v_matches, msg = _find_docs_by_keyword(v_docs, vendor_keyword, "vendor", "list_vendors()")
    if not v_matches:
        return msg

    v_doc       = v_matches[0]
    vendor_name = get_vendor_name(v_doc)

    logger.info(f"[tools] Reverse lookup: requirements for '{vendor_name}'")

    scored = []
    for req_doc in req_docs:
        res = score_vendor_against_requirement(req_doc, v_doc)
        scored.append({
            "req_name":   get_doc_display_name(req_doc),
            "req_id":     str(req_doc.get("record_id", req_doc.get("_id", ""))),
            "score":      res["total_score"],
            "dimensions": res["dimensions"],
            "gaps":       res["gaps"],
        })

    scored.sort(key=lambda x: x["score"], reverse=True)

    # Reuse vendor_across_requirements formatter (same structure)
    return format_vendor_across_requirements(vendor_name, scored)


# ─────────────────────────────────────────────────────────────
# ── CROSS-COLLECTION TOOLS ───────────────────────────────────
# ─────────────────────────────────────────────────────────────

def get_requirement_with_vendor_context(requirement_keyword: str) -> str:
    """
    Fetch a requirement's full details PLUS the top-matching vendor
    contacts and any related quotations — all in one call.

    This is the ideal preparation tool before composing an RFQ email,
    giving the email agent everything it needs.

    Use when the user asks:
      "get printer requirement and send it to Bansal Technology",
      "prepare RFQ for laptop requirement",
      "get all context for server requirement before emailing vendors"
    """
    req_docs, err = _safe_fetch_all(_req_col, "requirements")
    if err:
        return err
    v_docs,   _   = _safe_fetch_all(_vendor_col, "vendors")
    q_docs,   _   = _safe_fetch_all(_quotation_col, "quotations")

    req_matches, msg = _find_docs_by_keyword(req_docs, requirement_keyword,
                                              "requirement", "list_requirements()")
    if not req_matches:
        return msg

    req_doc  = req_matches[0]
    req_name = get_doc_display_name(req_doc)
    req_sd   = req_doc.get("structured_data") or {}

    lines = [
        f"REQUIREMENT + CONTEXT: {req_name}",
        "=" * 62,
        "\n📋  REQUIREMENT DETAILS:",
    ]
    if req_sd:
        lines.append(json.dumps(req_sd, indent=2, ensure_ascii=False)[:3000])
    else:
        preview = (req_doc.get("text_content") or "")[:1500]
        lines.append(preview or "(no content)")

    lines += [
        f"\nFile Path : {req_doc.get('file_path', 'N/A')}",
        f"Record ID : {req_doc.get('record_id', '')}",
    ]

    # Top vendors by score
    if v_docs:
        scored = []
        for v in v_docs:
            res = score_vendor_against_requirement(req_doc, v)
            sd  = v.get("structured_data") or {}
            res["email"]   = extract_field(sd, "contact_email", "email", default="Not found")
            res["contact"] = extract_field(sd, "contact_person", "contact_name", default="N/A")
            res["phone"]   = extract_field(sd, "contact_phone", "phone", default="N/A")
            scored.append(res)
        scored.sort(key=lambda x: x["total_score"], reverse=True)

        lines += ["", "🏢  TOP VENDORS (by match score):"]
        for v in scored[:5]:
            lines.append(
                f"  • {v['vendor_name']} ({v['total_score']}%) "
                f"— {v['email']} | {v['contact']} | {v['phone']}"
            )
    else:
        lines.append("\n🏢  VENDORS: None found in database.")

    # Related quotations
    q_matches = [d for d in q_docs if keyword_in_doc(requirement_keyword, d)]
    if q_matches:
        lines += [f"", f"📄  RELATED QUOTATIONS ({len(q_matches)} found):"]
        for q in q_matches:
            sd    = q.get("structured_data") or {}
            total = extract_field(sd, "grand_total", "total_amount", "total", default="N/A")
            curr  = extract_field(sd, "currency", default="INR")
            lines.append(
                f"  • {get_doc_display_name(q)} — Vendor: {get_vendor_name(q)}, "
                f"Total: {curr} {total}"
            )
    else:
        lines.append("\n📄  RELATED QUOTATIONS: None found.")

    return "\n".join(lines)


def search_across_all(keyword: str) -> str:
    """
    Search the keyword across requirements, vendors, AND quotations
    simultaneously and return a summarised hit list.

    Use when the user's intent is unclear or they want a global search:
      "anything related to Dell",
      "search everything for laptop",
      "find all records mentioning Bansal"
    """
    results: list[str] = []

    for label, col_fn in (
        ("Requirements", _req_col),
        ("Vendors",      _vendor_col),
        ("Quotations",   _quotation_col),
    ):
        docs, err = _safe_fetch_all(col_fn, label.lower())
        if err:
            results.append(f"{label}: {err}")
            continue
        hits = [d for d in docs if keyword_in_doc(keyword, d)]
        if hits:
            results.append(f"\n{label} ({len(hits)} match(es)):")
            for d in hits:
                results.append(f"  • {get_doc_display_name(d)}  [ID: {d.get('record_id', '')}]")
        else:
            results.append(f"\n{label}: no matches for '{keyword}'")

    return f"SEARCH RESULTS — '{keyword}'\n" + "=" * 50 + "\n" + "\n".join(results)


def get_vendor_full_profile(vendor_keyword: str) -> str:
    """
    Get a comprehensive 360° profile of a vendor: contact information,
    their match scores against all requirements, and all quotations
    they have submitted — consolidated in one view.

    Use when the user asks:
      "give me the full profile of Bansal Technology",
      "everything about Code Lab vendor",
      "deep dive on Chitkara vendor — contact, scores, quotes"
    """
    v_docs, err = _safe_fetch_all(_vendor_col, "vendors")
    if err:
        return err
    req_docs, _ = _safe_fetch_all(_req_col, "requirements")
    q_docs,   _ = _safe_fetch_all(_quotation_col, "quotations")

    v_matches, msg = _find_docs_by_keyword(v_docs, vendor_keyword, "vendor", "list_vendors()")
    if not v_matches:
        return msg

    v_doc       = v_matches[0]
    vendor_name = get_vendor_name(v_doc)
    sd          = v_doc.get("structured_data") or {}

    # Contact info dict
    certs = extract_field(sd, "certifications", "certificates", default="N/A")
    if isinstance(certs, list):
        certs = ", ".join(str(c) for c in certs[:4])

    contact_info = {
        "company": extract_field(sd, "company_name", "vendor_name",
                                 "organization", default=vendor_name),
        "person":  extract_field(sd, "contact_person", "contact_name",
                                 "person", "representative", default="N/A"),
        "email":   extract_field(sd, "contact_email", "email", "email_address",
                                 "mail", default="NOT FOUND"),
        "phone":   extract_field(sd, "contact_phone", "phone", "mobile",
                                 "telephone", default="N/A"),
        "address": extract_field(sd, "company_address", "address",
                                 "location", "office_address", default="N/A"),
        "website": extract_field(sd, "website", "web", "url", default="N/A"),
        "certs":   certs,
    }

    # Requirement scores
    req_scores = []
    for req_doc in req_docs:
        res = score_vendor_against_requirement(req_doc, v_doc)
        req_scores.append({
            "req_name": get_doc_display_name(req_doc),
            "score":    res["total_score"],
        })

    # Vendor's quotations
    v_norm = normalize_text(vendor_name)
    vendor_quotations = [
        q for q in q_docs
        if v_norm in normalize_text(get_vendor_name(q))
        or keyword_in_doc(vendor_keyword, q)
    ]

    quotation_rows = []
    for q in vendor_quotations:
        q_sd     = q.get("structured_data") or {}
        total    = extract_field(q_sd, "grand_total", "total_amount", "total", default="N/A")
        currency = extract_field(q_sd, "currency", default="INR")
        validity = extract_field(q_sd, "validity_date", "valid_until", default="N/A")
        items    = extract_field(q_sd, "line_items", "items", "products", default=[])
        n_items  = len(items) if isinstance(items, list) else "N/A"
        quotation_rows.append({
            "file_name": q.get("file_name", get_doc_display_name(q)),
            "total":     f"{currency} {total}",
            "validity":  validity,
            "n_items":   n_items,
        })

    logger.info(
        f"[tools] Full profile: '{vendor_name}' | "
        f"{len(req_scores)} req scores | {len(quotation_rows)} quotation(s)"
    )

    return format_vendor_full_profile(vendor_name, contact_info, req_scores, quotation_rows)


def get_procurement_summary() -> str:
    """
    Generate a full procurement dashboard — counts, coverage, best vendor
    per requirement, and top vendors overall.

    Use when the user asks:
      "give me a procurement overview",
      "what is the current status of our procurement",
      "procurement dashboard",
      "summary of requirements, vendors, and quotations"
    """
    req_docs, err = _safe_fetch_all(_req_col, "requirements")
    if err:
        return err
    v_docs,   err = _safe_fetch_all(_vendor_col, "vendors")
    if err:
        return err
    q_docs,   _   = _safe_fetch_all(_quotation_col, "quotations")

    if not req_docs:
        return "No requirements found in the database."

    # Best vendor per requirement
    best_per_req = []
    for req_doc in req_docs:
        req_name = get_doc_display_name(req_doc)
        if v_docs:
            scored = [score_vendor_against_requirement(req_doc, v) for v in v_docs]
            scored.sort(key=lambda x: x["total_score"], reverse=True)
            best = scored[0]
            best_per_req.append({
                "req_name":    req_name,
                "vendor_name": best["vendor_name"],
                "score":       best["total_score"],
            })
        else:
            best_per_req.append({
                "req_name": req_name, "vendor_name": "N/A", "score": 0
            })

    # Top vendors overall (aggregate)
    vendor_aggregates = []
    for v_doc in v_docs:
        scores = [
            score_vendor_against_requirement(req_doc, v_doc)["total_score"]
            for req_doc in req_docs
        ]
        vendor_aggregates.append({
            "vendor_name": get_vendor_name(v_doc),
            "avg_score":   round(sum(scores) / len(scores)) if scores else 0,
        })
    vendor_aggregates.sort(key=lambda x: x["avg_score"], reverse=True)

    # Coverage: requirements that have at least one quotation
    req_names_set = {normalize_text(get_doc_display_name(r)) for r in req_docs}
    quoted_set: set[str] = set()
    for q in q_docs:
        q_text = normalize_text(get_all_text(q))
        for rn in req_names_set:
            if any(word in q_text for word in rn.split() if len(word) > 3):
                quoted_set.add(rn)

    n_unquoted   = len(req_docs) - len(quoted_set)
    coverage_pct = round(len(quoted_set) / len(req_docs) * 100) if req_docs else 0

    summary_data = {
        "n_requirements": len(req_docs),
        "n_vendors":      len(v_docs),
        "n_quotations":   len(q_docs),
        "n_unquoted":     n_unquoted,
        "coverage_pct":   coverage_pct,
        "best_per_req":   best_per_req,
        "top_vendors":    vendor_aggregates[:5],
    }

    logger.info(
        f"[tools] Procurement summary | reqs={len(req_docs)} | "
        f"vendors={len(v_docs)} | quotations={len(q_docs)}"
    )

    return format_procurement_summary(summary_data)


def find_unquoted_requirements() -> str:
    """
    Find requirements that have NO quotation received yet and suggest
    the best vendor to approach for each one.

    Use when the user asks:
      "which requirements don't have any quotation",
      "show me requirements with no quote",
      "what is still pending for quotation",
      "which requirements should we send RFQ for"
    """
    req_docs, err = _safe_fetch_all(_req_col, "requirements")
    if err:
        return err
    q_docs,   _   = _safe_fetch_all(_quotation_col, "quotations")
    v_docs,   _   = _safe_fetch_all(_vendor_col, "vendors")

    if not req_docs:
        return "No requirements found in the database."

    # Determine which requirements have a quotation
    quoted_req_names: set[str] = set()
    for q in q_docs:
        q_text = normalize_text(get_all_text(q))
        for req_doc in req_docs:
            req_name = normalize_text(get_doc_display_name(req_doc))
            core_words = [w for w in req_name.split() if len(w) > 3]
            if any(w in q_text for w in core_words):
                quoted_req_names.add(req_name)

    unquoted_docs = [
        r for r in req_docs
        if normalize_text(get_doc_display_name(r)) not in quoted_req_names
    ]

    # Suggest top vendor for each unquoted requirement
    unquoted_data = []
    for req_doc in unquoted_docs:
        top_vendor, top_score = "N/A", 0
        if v_docs:
            scored = [score_vendor_against_requirement(req_doc, v) for v in v_docs]
            scored.sort(key=lambda x: x["total_score"], reverse=True)
            top_vendor = scored[0]["vendor_name"]
            top_score  = scored[0]["total_score"]
        unquoted_data.append({
            "req_name":   get_doc_display_name(req_doc),
            "req_id":     str(req_doc.get("record_id", req_doc.get("_id", ""))),
            "top_vendor": top_vendor,
            "top_score":  top_score,
        })

    logger.info(
        f"[tools] Unquoted requirements: {len(unquoted_docs)}/{len(req_docs)}"
    )

    return format_unquoted_requirements(unquoted_data, v_docs)


def get_rfq_readiness_report() -> str:
    """
    Generate a per-requirement RFQ readiness report showing:
      - Which vendors have already submitted quotations
      - Which vendors have NOT yet quoted (potential RFQ targets)
      - The best matching vendor for each requirement

    Use when the user asks:
      "are we ready to send RFQs",
      "show me the RFQ status for all requirements",
      "which vendors haven't submitted a quote yet",
      "RFQ readiness report",
      "procurement pipeline status"
    """
    req_docs, err = _safe_fetch_all(_req_col, "requirements")
    if err:
        return err
    v_docs,   err = _safe_fetch_all(_vendor_col, "vendors")
    if err:
        return err
    q_docs,   _   = _safe_fetch_all(_quotation_col, "quotations")

    if not req_docs:
        return "No requirements found in the database."

    logger.info(
        f"[tools] RFQ readiness | {len(req_docs)} req(s) | "
        f"{len(v_docs)} vendor(s) | {len(q_docs)} quotation(s)"
    )

    # Build map: vendor_name → quotation docs they submitted
    vendor_quotation_map: dict[str, list[dict]] = {}
    for q in q_docs:
        vname = normalize_text(get_vendor_name(q))
        vendor_quotation_map.setdefault(vname, []).append(q)

    all_vendor_names = {normalize_text(get_vendor_name(v)): get_vendor_name(v)
                        for v in v_docs}

    rfq_data = []
    for req_doc in req_docs:
        req_name = get_doc_display_name(req_doc)
        req_text = normalize_text(get_all_text(req_doc))
        req_core = [w for w in normalize_text(req_name).split() if len(w) > 3]

        # Vendors that have quoted for this requirement
        quoted_vendors = []
        for vname_norm, q_list in vendor_quotation_map.items():
            for q in q_list:
                q_text = normalize_text(get_all_text(q))
                if any(w in q_text for w in req_core):
                    quoted_vendors.append(all_vendor_names.get(vname_norm, vname_norm))
                    break

        # Vendors that have NOT quoted
        quoted_norms    = {normalize_text(v) for v in quoted_vendors}
        unquoted_vendors = [
            display_name
            for norm, display_name in all_vendor_names.items()
            if norm not in quoted_norms
        ]

        # Best matching vendor overall
        best_vendor, best_score = "N/A", 0
        if v_docs:
            scored = [score_vendor_against_requirement(req_doc, v) for v in v_docs]
            scored.sort(key=lambda x: x["total_score"], reverse=True)
            best_vendor = scored[0]["vendor_name"]
            best_score  = scored[0]["total_score"]

        status = "covered" if quoted_vendors else "none"

        rfq_data.append({
            "req_name":         req_name,
            "quoted_vendors":   quoted_vendors,
            "unquoted_vendors": unquoted_vendors,
            "best_match_vendor": best_vendor,
            "best_match_score":  best_score,
            "status":            status,
        })

    return format_rfq_readiness(rfq_data)


# ─────────────────────────────────────────────────────────────
# TOOL REGISTRY
# ─────────────────────────────────────────────────────────────

ROOT_TOOLS: list = [
    # ── Requirements ────────────────────────────────────────
    list_requirements,
    get_requirement,
    count_requirements_by_category,

    # ── Vendors ─────────────────────────────────────────────
    list_vendors,
    get_vendor,
    get_vendor_contact_info,

    # ── Quotations ──────────────────────────────────────────
    list_quotations,
    get_quotation,
    get_quotations_by_vendor,

    # ── Scoring (per-requirement) ────────────────────────────
    score_vendors_for_requirement,
    generate_full_score_matrix,
    check_quotation_coverage,
    compare_quotations_for_requirement,

    # ── Scoring (aggregate / extended) ──────────────────────
    rank_vendors_overall,
    score_vendor_across_requirements,
    compare_vendors_head_to_head,
    find_best_vendor_for_category,

    # ── Matching (reverse) ───────────────────────────────────
    find_requirements_for_vendor,

    # ── Cross-collection ────────────────────────────────────
    get_requirement_with_vendor_context,
    search_across_all,
    get_vendor_full_profile,
    get_procurement_summary,
    find_unquoted_requirements,
    get_rfq_readiness_report,
]

