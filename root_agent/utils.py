# root_agent/utils.py
# ─────────────────────────────────────────────────────────────
# Shared helpers for Jarvis root-agent tools:
#   - Text normalisation & keyword matching
#   - Flexible field extraction (handles varied LLM-generated keys)
#   - 5-dimension vendor scoring engine
#   - Report formatters (score table, matrix, quotation coverage)
#   - Extended formatters for aggregate ranking, head-to-head,
#     procurement dashboard, RFQ readiness, and vendor profiles
# ─────────────────────────────────────────────────────────────

from __future__ import annotations

import json
import re
from typing import Any

# ─────────────────────────────────────────────────────────────
# TEXT HELPERS
# ─────────────────────────────────────────────────────────────

def normalize_text(text: str) -> str:
    """Lowercase, collapse whitespace, strip — ready for keyword matching."""
    if not text:
        return ""
    return re.sub(r"\s+", " ", str(text).lower().strip())


def extract_field(data: dict, *field_names: str, default: Any = None) -> Any:
    """
    Try multiple field-name candidates (case-insensitive, underscore/space/dash
    variants) to extract a value from a dict that may have been structured by an
    LLM using slightly different key names each time.

    Returns the first match found, or *default* when nothing matches.
    """
    if not isinstance(data, dict):
        return default

    for name in field_names:
        # 1. Exact match
        if name in data:
            return data[name]

        # 2. Case-insensitive exact
        lower = name.lower()
        for k, v in data.items():
            if str(k).lower() == lower:
                return v

        # 3. Common separators swapped
        variants = {
            name.replace(" ", "_"),
            name.replace("_", " "),
            name.replace("-", "_"),
            name.replace("_", "-"),
        }
        for variant in variants:
            if variant in data:
                return data[variant]
            for k, v in data.items():
                if str(k).lower() == variant.lower():
                    return v

    return default


def get_all_text(doc: dict) -> str:
    """
    Concatenate every text-bearing field in a document into one
    searchable string (normalised to lowercase).
    """
    parts: list[str] = []
    if doc.get("text_content"):
        parts.append(str(doc["text_content"]))
    if doc.get("structured_data") and isinstance(doc["structured_data"], dict):
        try:
            parts.append(json.dumps(doc["structured_data"], ensure_ascii=False))
        except Exception:
            pass
    if doc.get("file_name"):
        parts.append(str(doc["file_name"]))
    return normalize_text(" ".join(parts))


def keyword_in_doc(keyword: str, doc: dict) -> bool:
    """Return True if *keyword* (normalised) appears anywhere in *doc*."""
    return normalize_text(keyword) in get_all_text(doc)


def get_doc_display_name(doc: dict) -> str:
    """Extract a human-readable title from any domain document."""
    sd = doc.get("structured_data") or {}
    for field in (
        "requirement_title", "title", "name", "subject",
        "vendor_name", "quotation_number", "quote_number",
        "category", "item", "product",
    ):
        val = extract_field(sd, field)
        if val and isinstance(val, str):
            return val.strip()
    return doc.get("file_name", str(doc.get("record_id", "Unknown")))


def get_vendor_name(doc: dict) -> str:
    """Extract vendor name from a vendor or quotation document."""
    sd = doc.get("structured_data") or {}
    for field in (
        "vendor_name", "company_name", "name",
        "supplier_name", "supplier", "organization",
    ):
        val = extract_field(sd, field)
        if val and isinstance(val, str):
            return val.strip()
    fn = doc.get("file_name", "")
    if fn:
        return fn.replace("_", " ").replace("-", " ").rsplit(".", 1)[0]
    return "Unknown Vendor"


# ─────────────────────────────────────────────────────────────
# CATEGORY TAXONOMY
# ─────────────────────────────────────────────────────────────

_CATEGORY_MAP: dict[str, list[str]] = {
    "laptop":      ["laptop", "notebook", "ultrabook", "portable computer", "chromebook"],
    "desktop":     ["desktop", "workstation", "personal computer", " pc ", "all-in-one", "aio"],
    "server":      ["server", "rack server", "blade server", "tower server", "poweredge", "proliant"],
    "printer":     ["printer", "printing", "laser printer", "inkjet", "multifunction printer", "mfp", "mfd"],
    "scanner":     ["scanner", "scanning", "document scanner", "flatbed"],
    "projector":   ["projector", "projection system", "beam", "pico projector"],
    "monitor":     ["monitor", "display", "screen", "led display", "lcd"],
    "networking":  ["router", "switch", "firewall", "access point", "wifi", "wireless", "ethernet", "network"],
    "storage":     ["storage", "nas", "san", "hard disk", "hdd", "ssd", "flash", "external drive"],
    "ups":         ["ups", "uninterruptible power", "battery backup", "power supply", "inverter"],
    "tablet":      ["tablet", "ipad", "android tablet", "slate"],
    "mobile":      ["mobile", "smartphone", "phone", "handset"],
    "camera":      ["camera", "cctv", "surveillance", "webcam", "ip camera", "ptz"],
    "ac":          ["air conditioner", "ac unit", "hvac", "cooling unit", "split ac", "cassette ac"],
    "furniture":   ["furniture", "chair", "table", "desk", "cabinet", "workstation furniture", "shelf"],
    "stationery":  ["stationery", "paper", "pen", "pencil", "folder", "binder", "toner", "cartridge"],
    "software":    ["software", "license", "subscription", "saas", "erp", "crm", "antivirus"],
    "it":          ["it equipment", "information technology", "peripherals", "accessories"],
    "printing_consumables": ["toner", "ink cartridge", "drum", "ribbon"],
    "data_center": ["data center", "datacenter", "rack", "pdu", "kvm"],
    "gpu":         ["gpu", "graphics card", "graphics processing unit", "vga", "cuda",
                    "rtx", "gtx", "quadro", "radeon", "geforce"],
}

_CERT_KEYWORDS: list[str] = [
    "iso 9001", "iso 14001", "iso 27001", "iso 45001", "iso 50001",
    "bis certified", "ce marked", "ce certified", "ul listed", "rohs compliant",
    "energy star", "cmmi", "soc 2", "pci dss", "gdpr",
    "iso certified", "government empanelled", "gem seller", "nsic",
    "msme registered", "startup india", "make in india",
]


def _detect_categories(text: str) -> set[str]:
    """Return the set of category keys whose keywords appear in *text*."""
    t = normalize_text(text)
    return {cat for cat, kws in _CATEGORY_MAP.items() if any(kw in t for kw in kws)}


# ─────────────────────────────────────────────────────────────
# SCORING DIMENSION HELPERS
# ─────────────────────────────────────────────────────────────

def _score_category_match(req_text: str, vendor_text: str) -> tuple[int, str]:
    req_cats    = _detect_categories(req_text)
    vendor_cats = _detect_categories(vendor_text)

    if not req_cats:
        return 15, "Requirement categories not clearly specified — neutral score applied."

    overlap = req_cats & vendor_cats
    missing  = req_cats - vendor_cats

    if not missing:                          # full coverage
        return 30, f"Exact category match: {', '.join(sorted(overlap))}."
    if overlap:                              # partial
        pct = round(len(overlap) / len(req_cats) * 100)
        return 15, (
            f"Partial match ({pct}%): vendor covers {', '.join(sorted(overlap))}; "
            f"missing: {', '.join(sorted(missing))}."
        )
    # no overlap
    v_cats_str = ", ".join(sorted(vendor_cats)) or "not specified"
    return 0, (
        f"No category overlap. Requirement needs: {', '.join(sorted(req_cats))}; "
        f"vendor offers: {v_cats_str}."
    )


# Spec feature groups to probe
_SPEC_GROUPS: list[tuple[str, list[str]]] = [
    ("processor", ["processor", "cpu", "core i", "core i3", "core i5", "core i7", "core i9",
                   "ryzen", "xeon", "intel", "amd", "ghz"]),
    ("ram",       ["ram", " memory", "ddr", "gb ram", "gb memory"]),
    ("storage",   ["ssd", "hdd", "storage", "hard disk", " tb ", " gb ", "nvme", "m.2"]),
    ("display",   ["display", "screen size", " inch", "resolution", "fhd", "uhd", "4k", "ips", "oled"]),
    ("os",        ["windows", "linux", "ubuntu", "centos", "rhel", "macos"]),
    ("warranty",  ["warranty", "guarantee", "onsite support", "amc"]),
    ("battery",   ["battery", "mah", "wh", "hours battery"]),
    ("ports",     ["usb", "hdmi", "thunderbolt", "type-c", "ethernet port"]),
    ("gpu",       ["gpu", "graphics", "cuda", "rtx", "gtx", "vram", "quadro", "geforce", "radeon"]),
]


def _score_spec_alignment(
    req_text: str, vendor_text: str
) -> tuple[int, str, list[str]]:
    """Returns (score/30, reason, gaps)."""
    req_specs     = set()
    vendor_specs  = set()

    for name, kws in _SPEC_GROUPS:
        if any(k in req_text for k in kws):
            req_specs.add(name)
        if any(k in vendor_text for k in kws):
            vendor_specs.add(name)

    if not req_specs:
        return 15, "Specification detail not found in requirement — neutral score.", []

    matched = req_specs & vendor_specs
    missing  = req_specs - vendor_specs
    pct      = len(matched) / len(req_specs)

    gaps = [f"Verify spec: {g}" for g in sorted(missing)] if missing else []

    if pct >= 1.0:
        return 30, f"All required specs covered: {', '.join(sorted(matched))}.", []
    if pct >= 0.7:
        return 22, (
            f"Most specs covered ({int(pct*100)}%): {', '.join(sorted(matched))}; "
            f"not stated: {', '.join(sorted(missing))}."
        ), gaps
    if pct >= 0.3:
        return 12, (
            f"Partial spec coverage ({int(pct*100)}%): {', '.join(sorted(matched))} matched; "
            f"{', '.join(sorted(missing))} missing."
        ), gaps
    return 0, f"Required specs ({', '.join(sorted(req_specs))}) not found in vendor document.", gaps


# ── Budget extraction ────────────────────────────────────────

_AMOUNT_PATTERNS: list[tuple[re.Pattern, float]] = [
    (re.compile(r"(\d+(?:\.\d+)?)\s*(?:crore|cr\.?)"),          1e7),
    (re.compile(r"(\d+(?:\.\d+)?)\s*(?:lakh|lac|l\.?)"),        1e5),
    (re.compile(r"(\d+(?:\.\d+)?)\s*(?:thousand|k)"),           1e3),
    (re.compile(r"(?:inr|rs\.?|₹)\s*(\d[\d,]+)"),               1.0),
    (re.compile(r"(?:budget|amount|cost|price|total)[^\d]*(\d[\d,]+)"), 1.0),
]


def _extract_amount_inr(text: str) -> float | None:
    """Try to extract a monetary amount (INR) from text. Returns float or None."""
    t = text.lower()
    for pattern, multiplier in _AMOUNT_PATTERNS:
        m = pattern.search(t)
        if m:
            raw = float(m.group(1).replace(",", ""))
            if multiplier == 1.0 and raw < 1000:
                continue          # probably not a meaningful amount
            return raw * multiplier
    return None


def _score_budget_fit(req_text: str, vendor_text: str) -> tuple[int, str, str]:
    """Returns (score/20, reason, gap_hint)."""
    req_budget   = _extract_amount_inr(req_text)
    vendor_price = _extract_amount_inr(vendor_text)

    if req_budget is None:
        return 10, "Budget not specified in requirement — neutral score.", ""
    if vendor_price is None:
        return 10, "Vendor pricing not quantified in document — neutral score.", \
               "Verify vendor unit pricing and total cost."

    ratio = vendor_price / req_budget if req_budget else 1.0

    if ratio <= 1.0:
        return 20, (
            f"Within budget (vendor ≈ ₹{vendor_price:,.0f}; budget ₹{req_budget:,.0f})."
        ), ""
    if ratio <= 1.10:
        over = int((ratio - 1) * 100)
        return 14, f"Slightly over budget by ~{over}%; negotiate pricing.", "Negotiate 5–10% discount."
    if ratio <= 1.25:
        over = int((ratio - 1) * 100)
        return 8, f"Over budget by ~{over}%; significant negotiation needed.", \
               "Request revised quote / volume discount."
    return 0, f"Exceeds budget by ~{int((ratio-1)*100)}% — likely unaffordable.", \
           "Review budget allocation or seek alternative vendor."


def _score_certifications(vendor_text: str) -> tuple[int, str]:
    found = [c for c in _CERT_KEYWORDS if c in vendor_text]
    _high = {"iso 9001", "iso 27001", "cmmi", "soc 2", "bis certified"}
    has_high = any(c in found for c in _high)

    if has_high and len(found) >= 2:
        return 10, f"Strong certifications: {', '.join(found[:4])}."
    if found:
        return 6, f"Some certifications present: {', '.join(found[:3])}."
    return 3, "No certifications explicitly mentioned in vendor document."


def _score_delivery_payment(req_text: str, vendor_text: str) -> tuple[int, str, str]:
    _req_has_deadline = any(
        kw in req_text for kw in
        ["deadline", "delivery by", "required by", "lead time", "days", "weeks", "before "]
    )
    _vendor_has_delivery = any(
        kw in vendor_text for kw in
        ["delivery", "lead time", "dispatch", "days", "weeks", "ship", "logistics"]
    )
    _vendor_has_payment = any(
        kw in vendor_text for kw in
        ["payment", "credit", "advance", "net30", "net60", "45 days", "30 days", "60 days",
         "letter of credit", "lc", "bank transfer", "cheque"]
    )

    if not _req_has_deadline:
        if _vendor_has_delivery and _vendor_has_payment:
            return 10, "Delivery and payment terms clearly stated; no deadline constraint in requirement.", ""
        if _vendor_has_delivery or _vendor_has_payment:
            return 7, "Partial delivery/payment info in vendor document.", \
                   "Confirm complete delivery and payment terms."
        return 5, "No delivery/payment terms found; neutral score.", \
               "Request delivery schedule and payment terms from vendor."

    # requirement has a deadline
    if _vendor_has_delivery and _vendor_has_payment:
        return 10, "Vendor has delivery and payment terms; verify exact dates align with requirement.", \
               "Confirm vendor delivery date meets requirement deadline."
    if _vendor_has_delivery:
        return 7, "Delivery timeline mentioned; payment terms not detailed.", \
               "Clarify payment terms and confirm delivery timeline."
    return 3, "Vendor delivery terms not found; requirement has a deadline — high risk.", \
           "URGENT: Verify vendor can meet the delivery deadline."


# ─────────────────────────────────────────────────────────────
# MASTER SCORING FUNCTION
# ─────────────────────────────────────────────────────────────

def score_vendor_against_requirement(req_doc: dict, vendor_doc: dict) -> dict:
    """
    Score *vendor_doc* against *req_doc* across 5 dimensions (max 100).

    Returns a dict with:
      vendor_name, vendor_record_id, requirement_name, total_score,
      match_percent, dimensions{score, max, reason}, gaps[]
    """
    req_text    = get_all_text(req_doc)
    vendor_text = get_all_text(vendor_doc)

    cat_score,  cat_reason             = _score_category_match(req_text, vendor_text)
    spec_score, spec_reason, spec_gaps = _score_spec_alignment(req_text, vendor_text)
    bgt_score,  bgt_reason,  bgt_gap   = _score_budget_fit(req_text, vendor_text)
    cert_score, cert_reason            = _score_certifications(vendor_text)
    del_score,  del_reason,  del_gap   = _score_delivery_payment(req_text, vendor_text)

    total = cat_score + spec_score + bgt_score + cert_score + del_score
    gaps  = [g for g in ([bgt_gap, del_gap] + spec_gaps) if g]

    return {
        "vendor_name":       get_vendor_name(vendor_doc),
        "vendor_record_id":  str(vendor_doc.get("record_id", vendor_doc.get("_id", ""))),
        "requirement_name":  get_doc_display_name(req_doc),
        "total_score":       total,
        "match_percent":     total,
        "dimensions": {
            "category_match":  {"score": cat_score,  "max": 30, "reason": cat_reason},
            "spec_alignment":  {"score": spec_score,  "max": 30, "reason": spec_reason},
            "budget_fit":      {"score": bgt_score,   "max": 20, "reason": bgt_reason},
            "certifications":  {"score": cert_score,  "max": 10, "reason": cert_reason},
            "delivery_payment":{"score": del_score,   "max": 10, "reason": del_reason},
        },
        "gaps": gaps,
    }


# ─────────────────────────────────────────────────────────────
# QUOTATION ↔ REQUIREMENTS COVERAGE
# ─────────────────────────────────────────────────────────────

_ITEM_KEYWORDS: list[str] = [
    "laptop", "desktop", "server", "printer", "scanner", "monitor",
    "projector", "tablet", "mobile", "camera", "ups", "router", "switch",
    "firewall", "storage", "nas", "keyboard", "mouse", "headset",
    "furniture", "chair", "table", "desk", "stationery", "software",
    "license", "ac", "air conditioner", "gpu", "graphics card",
]


def check_quotation_requirement_coverage(
    quotation_doc: dict,
    requirement_docs: list[dict],
) -> list[dict]:
    """
    For each requirement, check whether the quotation likely covers it.

    Returns list[dict] sorted by coverage_score desc with keys:
      requirement_name, requirement_record_id, can_fulfill,
      coverage_score, category_overlap, matched_items, reason
    """
    q_text    = get_all_text(quotation_doc)
    q_cats    = _detect_categories(q_text)
    q_sd      = quotation_doc.get("structured_data") or {}

    # Gather item descriptions from quotation line items
    line_items = extract_field(q_sd, "line_items", "items", "products", "line items", default=[])
    item_desc_text = ""
    if isinstance(line_items, list):
        for item in line_items:
            if isinstance(item, dict):
                desc = extract_field(item, "item_description", "description",
                                     "product", "name", "item", default="")
                item_desc_text += f" {desc}"
    item_desc_norm = normalize_text(item_desc_text)

    results = []
    for req in requirement_docs:
        req_text = get_all_text(req)
        req_name = get_doc_display_name(req)
        req_cats = _detect_categories(req_text)

        cat_overlap   = req_cats & q_cats
        item_matches  = [kw for kw in _ITEM_KEYWORDS
                         if kw in req_text and (kw in q_text or kw in item_desc_norm)]

        score = 0
        reasons: list[str] = []

        if cat_overlap:
            score += 50
            reasons.append(f"Category overlap: {', '.join(sorted(cat_overlap))}.")
        if item_matches:
            item_pct = len(item_matches) / max(
                sum(1 for kw in _ITEM_KEYWORDS if kw in req_text), 1
            )
            score += int(item_pct * 50)
            reasons.append(f"Matching items: {', '.join(item_matches[:5])}.")

        results.append({
            "requirement_name":       req_name,
            "requirement_record_id":  str(req.get("record_id", req.get("_id", ""))),
            "can_fulfill":            score >= 40,
            "coverage_score":         min(score, 100),
            "category_overlap":       sorted(cat_overlap),
            "matched_items":          item_matches,
            "reason":                 " ".join(reasons) if reasons
                                      else "No overlap found between quotation and requirement.",
        })

    return sorted(results, key=lambda x: x["coverage_score"], reverse=True)


# ─────────────────────────────────────────────────────────────
# REPORT FORMATTERS  (original)
# ─────────────────────────────────────────────────────────────

def format_vendor_score_report(
    scored_vendors: list[dict],
    requirement_name: str,
    top_n: int | None = None,
) -> str:
    if not scored_vendors:
        return "No vendors available to score against this requirement."

    ranked = sorted(scored_vendors, key=lambda x: x["total_score"], reverse=True)
    display = ranked[:top_n] if top_n else ranked

    W = 68
    lines = [
        f"╔{'═'*W}╗",
        f"║  VENDOR MATCH REPORT — {requirement_name[:44]:<44}  ║",
        f"╠{'═'*W}╣",
        f"║  {'#':<4} {'Vendor':<24} {'Match%':<8} {'Cat':>4} {'Spec':>5} "
        f"{'Bgt':>4} {'Cert':>5} {'Del':>4}  ║",
        f"╠{'═'*W}╣",
    ]

    for i, v in enumerate(display, 1):
        d = v["dimensions"]
        lines.append(
            f"║  {i:<4} {v['vendor_name'][:24]:<24} {v['match_percent']:<7}%"
            f" {d['category_match']['score']:>4}"
            f" {d['spec_alignment']['score']:>5}"
            f" {d['budget_fit']['score']:>4}"
            f" {d['certifications']['score']:>5}"
            f" {d['delivery_payment']['score']:>4}  ║"
        )

    lines += [
        f"╠{'═'*W}╣",
        f"║  Max Points:                           "
        f"  {'30':>4} {'30':>5} {'20':>4} {'10':>5} {'10':>4}  ║",
        f"╚{'═'*W}╝",
        "",
        "DETAILED BREAKDOWN",
        "─" * W,
    ]

    for i, v in enumerate(display, 1):
        d = v["dimensions"]
        lines += [
            f"\n  Rank #{i} — {v['vendor_name']}  ({v['match_percent']}% overall match)",
            f"  ├─ Category Match  ({d['category_match']['score']:>2}/30): {d['category_match']['reason']}",
            f"  ├─ Spec Alignment  ({d['spec_alignment']['score']:>2}/30): {d['spec_alignment']['reason']}",
            f"  ├─ Budget Fit      ({d['budget_fit']['score']:>2}/20): {d['budget_fit']['reason']}",
            f"  ├─ Certifications  ({d['certifications']['score']:>2}/10): {d['certifications']['reason']}",
            f"  └─ Delivery+Pay    ({d['delivery_payment']['score']:>2}/10): {d['delivery_payment']['reason']}",
        ]
        if v["gaps"]:
            lines.append(f"     ⚠  Gaps to verify: {' | '.join(v['gaps'])}")

    if display:
        top = display[0]
        lines += [
            "",
            f"🏆  TOP PICK: {top['vendor_name']} — {top['match_percent']}% match",
        ]
        if len(display) > 1:
            runner = display[1]
            lines.append(
                f"🥈  RUNNER-UP: {runner['vendor_name']} — {runner['match_percent']}% match"
            )

    return "\n".join(lines)


def format_score_matrix(matrix: list[dict]) -> str:
    """Compact requirement × vendor score grid."""
    if not matrix:
        return "No data available for score matrix."

    col_w = 22
    lines = [
        "FULL SCORE MATRIX — All Requirements × All Vendors",
        "=" * 72,
        "(Scores: Category/30 + Spec/30 + Budget/20 + Cert/10 + Delivery/10 = Total%)",
    ]

    for entry in matrix:
        req_name = entry["requirement_name"]
        vendors  = sorted(entry["vendors"], key=lambda x: x["total_score"], reverse=True)

        lines += [
            "",
            f"📋  Requirement: {req_name}",
            f"    {'Vendor':<{col_w}} {'Total':>6}  {'Cat':>4} {'Spec':>5} "
            f"{'Bgt':>4} {'Cert':>5} {'Del':>4}",
            f"    {'─'*col_w} {'─'*6}  {'─'*4} {'─'*5} {'─'*4} {'─'*5} {'─'*4}",
        ]

        for v in vendors:
            d = v["dimensions"]
            lines.append(
                f"    {v['vendor_name'][:col_w]:<{col_w}} {v['total_score']:>5}%"
                f"  {d['category_match']['score']:>4}"
                f" {d['spec_alignment']['score']:>5}"
                f" {d['budget_fit']['score']:>4}"
                f" {d['certifications']['score']:>5}"
                f" {d['delivery_payment']['score']:>4}"
            )

        if vendors:
            best = vendors[0]
            lines.append(f"    → Best match: {best['vendor_name']} ({best['total_score']}%)")

    return "\n".join(lines)


def format_quotation_coverage_report(
    quotation_label: str,
    results: list[dict],
) -> str:
    can    = [r for r in results if r["can_fulfill"]]
    cannot = [r for r in results if not r["can_fulfill"]]

    lines = [
        f"QUOTATION COVERAGE REPORT",
        f"Quotation : {quotation_label}",
        "=" * 60,
        f"Total requirements checked : {len(results)}",
        f"Can potentially fulfill    : {len(can)}",
        f"Cannot fulfill             : {len(cannot)}",
    ]

    if can:
        lines += ["", "✅  REQUIREMENTS THIS QUOTATION CAN FULFILL:", "─" * 50]
        for r in can:
            lines.append(f"  • {r['requirement_name']}  (Coverage score: {r['coverage_score']}%)")
            lines.append(f"    {r['reason']}")

    if cannot:
        lines += ["", "❌  REQUIREMENTS NOT COVERED BY THIS QUOTATION:", "─" * 50]
        for r in cannot:
            lines.append(f"  • {r['requirement_name']}  (Coverage score: {r['coverage_score']}%)")
            if r["reason"] and "No overlap" not in r["reason"]:
                lines.append(f"    {r['reason']}")

    return "\n".join(lines)


def format_quotation_comparison(
    requirement_name: str,
    comparison_rows: list[dict],
) -> str:
    """Side-by-side quotation comparison for one requirement."""
    if not comparison_rows:
        return "No quotations found to compare."

    sorted_rows = sorted(comparison_rows, key=lambda x: x["score"]["total_score"], reverse=True)

    lines = [
        f"QUOTATION COMPARISON REPORT — {requirement_name}",
        "=" * 70,
        f"Quotations evaluated: {len(sorted_rows)}",
        "",
        f"{'#':<4} {'Vendor':<24} {'Amount':<22} {'Score':>6}  {'Delivery':<18}",
        f"{'─'*4} {'─'*24} {'─'*22} {'─'*6}  {'─'*18}",
    ]

    for i, r in enumerate(sorted_rows, 1):
        lines.append(
            f"{i:<4} {r['vendor_name'][:24]:<24} {str(r['grand_total'])[:22]:<22}"
            f" {r['score']['total_score']:>5}%  {str(r['delivery'])[:18]:<18}"
        )

    lines += ["", "DETAILED BREAKDOWN", "─" * 70]

    for i, r in enumerate(sorted_rows, 1):
        s = r["score"]
        d = s["dimensions"]
        lines += [
            f"\n  [{i}] {r['quotation_name']}  —  {r['vendor_name']}",
            f"      Amount         : {r['grand_total']}",
            f"      Match Score    : {s['total_score']}%",
            f"      Valid Until    : {r['validity']}",
            f"      Payment Terms  : {r['payment_terms']}",
            f"      Delivery       : {r['delivery']}",
            f"      Category Match : {d['category_match']['score']}/30 — {d['category_match']['reason']}",
            f"      Spec Alignment : {d['spec_alignment']['score']}/30 — {d['spec_alignment']['reason']}",
            f"      Budget Fit     : {d['budget_fit']['score']}/20  — {d['budget_fit']['reason']}",
        ]
        if s["gaps"]:
            lines.append(f"      ⚠  Gaps: {' | '.join(s['gaps'])}")

    if sorted_rows:
        best = sorted_rows[0]
        lines += [
            "",
            f"🏆  RECOMMENDED: {best['vendor_name']}  —  {best['grand_total']}"
            f"  ({best['score']['total_score']}% match)",
        ]

    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────
# EXTENDED FORMATTERS  (new tools)
# ─────────────────────────────────────────────────────────────

def format_overall_vendor_ranking(ranked_data: list[dict], top_n: int | None) -> str:
    """
    Format the aggregate vendor ranking across all requirements.

    ranked_data items:
      vendor_name, avg_score, max_score, min_score,
      n_requirements, n_relevant (score >= 50), per_req[{req_name, score}]
    """
    if not ranked_data:
        return "No vendors or requirements available for aggregate ranking."

    display = ranked_data[:top_n] if top_n else ranked_data
    W = 72

    lines = [
        f"╔{'═'*W}╗",
        f"║  OVERALL VENDOR RANKING — Aggregate Score Across All Requirements  ║",
        f"╠{'═'*W}╣",
        f"║  {'#':<4} {'Vendor':<26} {'Avg%':>5} {'Max':>4} {'Min':>4} {'Relevant/Total':>15}  ║",
        f"╠{'═'*W}╣",
    ]

    for i, v in enumerate(display, 1):
        rel = f"{v['n_relevant']}/{v['n_requirements']}"
        lines.append(
            f"║  {i:<4} {v['vendor_name'][:26]:<26} {v['avg_score']:>4}%"
            f" {v['max_score']:>4} {v['min_score']:>4} {rel:>15}  ║"
        )

    lines += [
        f"╠{'═'*W}╣",
        f"║  Relevant = score ≥ 50%.  Avg/Max/Min across all requirements.      ║",
        f"╚{'═'*W}╝",
        "",
        "PER-REQUIREMENT BREAKDOWN",
        "─" * W,
    ]

    for i, v in enumerate(display, 1):
        lines.append(f"\n  #{i}  {v['vendor_name']}  (avg {v['avg_score']}%)")
        for pr in v["per_req"]:
            bar   = "█" * (pr["score"] // 10) + "░" * (10 - pr["score"] // 10)
            lines.append(f"       {pr['req_name'][:40]:<40} {pr['score']:>3}%  {bar}")

    if display:
        lines += ["", f"🏆  OVERALL BEST VENDOR: {display[0]['vendor_name']} "
                      f"(avg {display[0]['avg_score']}% across "
                      f"{display[0]['n_requirements']} requirement(s))"]

    return "\n".join(lines)


def format_vendor_across_requirements(vendor_name: str, results: list[dict]) -> str:
    """
    Format how ONE vendor scores against each requirement.

    results items (sorted by score desc):
      req_name, score, dimensions{}, gaps[]
    """
    if not results:
        return f"No requirements available to score {vendor_name} against."

    W = 68
    lines = [
        f"╔{'═'*W}╗",
        f"║  {vendor_name[:60]:<60}  ║",
        f"║  SCORES ACROSS ALL REQUIREMENTS                                    ║",
        f"╠{'═'*W}╣",
        f"║  {'Requirement':<36} {'Score':>5}  {'Cat':>3} {'Spc':>4} {'Bgt':>4} {'Crt':>4} {'Del':>4}  ║",
        f"╠{'═'*W}╣",
    ]

    for r in results:
        d = r["dimensions"]
        lines.append(
            f"║  {r['req_name'][:36]:<36} {r['score']:>4}%"
            f"  {d['category_match']['score']:>3}"
            f" {d['spec_alignment']['score']:>4}"
            f" {d['budget_fit']['score']:>4}"
            f" {d['certifications']['score']:>4}"
            f" {d['delivery_payment']['score']:>4}  ║"
        )

    avg = round(sum(r["score"] for r in results) / len(results))
    relevant = sum(1 for r in results if r["score"] >= 50)

    lines += [
        f"╠{'═'*W}╣",
        f"║  Average Score: {avg}%   |   Relevant for {relevant}/{len(results)} requirements        ║",
        f"╚{'═'*W}╝",
        "",
        "DETAILED BREAKDOWN",
        "─" * W,
    ]

    for r in results:
        d = r["dimensions"]
        lines += [
            f"\n  {r['req_name']}  →  {r['score']}% match",
            f"  ├─ Category  ({d['category_match']['score']:>2}/30): {d['category_match']['reason']}",
            f"  ├─ Spec      ({d['spec_alignment']['score']:>2}/30): {d['spec_alignment']['reason']}",
            f"  ├─ Budget    ({d['budget_fit']['score']:>2}/20): {d['budget_fit']['reason']}",
            f"  ├─ Certs     ({d['certifications']['score']:>2}/10): {d['certifications']['reason']}",
            f"  └─ Delivery  ({d['delivery_payment']['score']:>2}/10): {d['delivery_payment']['reason']}",
        ]
        if r["gaps"]:
            lines.append(f"     ⚠  Gaps: {' | '.join(r['gaps'])}")

    return "\n".join(lines)


def format_head_to_head(
    v1_name: str,
    v2_name: str,
    scope_label: str,
    comparisons: list[dict],
) -> str:
    """
    Format head-to-head vendor comparison.

    comparisons items:
      req_name, v1_score, v2_score, winner ("v1"/"v2"/"tie"),
      v1_dims{}, v2_dims{}
    """
    if not comparisons:
        return "No data available for head-to-head comparison."

    v1_wins = sum(1 for c in comparisons if c["winner"] == "v1")
    v2_wins = sum(1 for c in comparisons if c["winner"] == "v2")
    ties    = sum(1 for c in comparisons if c["winner"] == "tie")
    v1_avg  = round(sum(c["v1_score"] for c in comparisons) / len(comparisons))
    v2_avg  = round(sum(c["v2_score"] for c in comparisons) / len(comparisons))

    W = 72
    lines = [
        f"╔{'═'*W}╗",
        f"║  HEAD-TO-HEAD COMPARISON — {scope_label[:43]:<43}  ║",
        f"╠{'═'*W}╣",
        f"║  {v1_name[:32]:<32}  vs  {v2_name[:32]:<32}  ║",
        f"╠{'═'*W}╣",
        f"║  {'Requirement':<32} {'V1 Score':>8} {'V2 Score':>9} {'Winner':>10}  ║",
        f"╠{'═'*W}╣",
    ]

    for c in comparisons:
        w = v1_name if c["winner"] == "v1" else (v2_name if c["winner"] == "v2" else "Tie")
        lines.append(
            f"║  {c['req_name'][:32]:<32} {c['v1_score']:>7}% {c['v2_score']:>8}%"
            f" {w[:10]:>10}  ║"
        )

    lines += [
        f"╠{'═'*W}╣",
        f"║  Average:                              {v1_avg:>7}% {v2_avg:>8}%              ║",
        f"╠{'═'*W}╣",
        f"║  Wins: {v1_name[:20]:<20} = {v1_wins:<3}  |  {v2_name[:20]:<20} = {v2_wins:<3}  |  Ties = {ties}  ║",
        f"╚{'═'*W}╝",
        "",
        "DIMENSION BREAKDOWN",
        "─" * W,
    ]

    dims = ["category_match", "spec_alignment", "budget_fit", "certifications", "delivery_payment"]
    dim_labels = {"category_match": "Category", "spec_alignment": "Spec",
                  "budget_fit": "Budget", "certifications": "Certs", "delivery_payment": "Delivery"}

    for c in comparisons:
        lines.append(f"\n  📋  {c['req_name']}")
        for dim in dims:
            s1 = c["v1_dims"].get(dim, {}).get("score", "?")
            s2 = c["v2_dims"].get(dim, {}).get("score", "?")
            arrow = "←" if s1 > s2 else ("→" if s2 > s1 else "=")
            lines.append(
                f"       {dim_labels[dim]:<10}  {v1_name[:18]:<18}: {s1:>3}  {arrow}  "
                f"{v2_name[:18]:<18}: {s2:>3}"
            )

    # Overall verdict
    if v1_avg > v2_avg:
        verdict = f"🏆  OVERALL WINNER: {v1_name} (avg {v1_avg}% vs {v2_avg}%)"
    elif v2_avg > v1_avg:
        verdict = f"🏆  OVERALL WINNER: {v2_name} (avg {v2_avg}% vs {v1_avg}%)"
    else:
        verdict = f"🤝  OVERALL: Even match — both vendors score {v1_avg}% average."

    lines += ["", verdict]
    return "\n".join(lines)


def format_vendor_full_profile(
    vendor_name: str,
    contact_info: dict,
    req_scores: list[dict],
    quotations: list[dict],
) -> str:
    """
    Full vendor profile: contact + requirement scores + quotations.

    req_scores items: req_name, score
    quotations items: file_name, total, validity, n_items
    """
    lines = [
        f"{'═'*68}",
        f"  VENDOR PROFILE — {vendor_name}",
        f"{'═'*68}",
        "",
        "📇  CONTACT INFORMATION",
        "─" * 50,
        f"  Company  : {contact_info.get('company', vendor_name)}",
        f"  Contact  : {contact_info.get('person', 'N/A')}",
        f"  Email    : {contact_info.get('email', 'NOT FOUND')}",
        f"  Phone    : {contact_info.get('phone', 'N/A')}",
        f"  Address  : {contact_info.get('address', 'N/A')}",
        f"  Website  : {contact_info.get('website', 'N/A')}",
        f"  Certs    : {contact_info.get('certs', 'N/A')}",
    ]

    # Requirement scores
    lines += ["", "📊  REQUIREMENT MATCH SCORES", "─" * 50]
    if req_scores:
        avg = round(sum(r["score"] for r in req_scores) / len(req_scores))
        rel = sum(1 for r in req_scores if r["score"] >= 50)
        lines.append(f"  Average: {avg}%   |   Relevant for {rel}/{len(req_scores)} requirement(s)")
        lines.append("")
        for r in sorted(req_scores, key=lambda x: x["score"], reverse=True):
            bar  = "█" * (r["score"] // 10) + "░" * (10 - r["score"] // 10)
            flag = "✅" if r["score"] >= 50 else "⚠️ "
            lines.append(f"  {flag} {r['req_name'][:40]:<40} {r['score']:>3}%  {bar}")
    else:
        lines.append("  No requirements found to score against.")

    # Quotations
    lines += ["", "📄  QUOTATIONS SUBMITTED", "─" * 50]
    if quotations:
        for q in quotations:
            lines.append(
                f"  • {q['file_name']}  |  {q['total']}  |  "
                f"Valid: {q['validity']}  |  Items: {q['n_items']}"
            )
    else:
        lines.append("  No quotations found from this vendor.")

    return "\n".join(lines)


def format_procurement_summary(data: dict) -> str:
    """
    Full procurement dashboard summary.

    data keys:
      n_requirements, n_vendors, n_quotations,
      n_unquoted, coverage_pct,
      best_per_req[{req_name, vendor_name, score}],
      top_vendors[{vendor_name, avg_score}]
    """
    lines = [
        "╔══════════════════════════════════════════════════════════════════╗",
        "║           PROCUREMENT DASHBOARD — JARVIS SUMMARY                ║",
        "╠══════════════════════════════════════════════════════════════════╣",
        f"║  Requirements   : {data['n_requirements']:<5}   Vendors     : {data['n_vendors']:<5}"
        f"  Quotations : {data['n_quotations']:<5}  ║",
        f"║  Unquoted Reqs  : {data['n_unquoted']:<5}   Coverage    : {data['coverage_pct']}%"
        f"                         ║",
        "╠══════════════════════════════════════════════════════════════════╣",
        "║  BEST VENDOR PER REQUIREMENT                                     ║",
        "╠══════════════════════════════════════════════════════════════════╣",
    ]

    for r in data.get("best_per_req", []):
        flag = "✅" if r["score"] >= 50 else "⚠️ "
        lines.append(
            f"║  {flag} {r['req_name'][:30]:<30}  →  {r['vendor_name'][:18]:<18} {r['score']:>3}%  ║"
        )

    lines += [
        "╠══════════════════════════════════════════════════════════════════╣",
        "║  TOP VENDORS OVERALL (by avg score)                              ║",
        "╠══════════════════════════════════════════════════════════════════╣",
    ]

    for i, v in enumerate(data.get("top_vendors", [])[:5], 1):
        lines.append(
            f"║  {i}. {v['vendor_name'][:40]:<40}  avg {v['avg_score']:>3}%         ║"
        )

    lines.append("╚══════════════════════════════════════════════════════════════════╝")
    return "\n".join(lines)


def format_unquoted_requirements(
    unquoted: list[dict],
    all_vendors: list[dict],
) -> str:
    """
    Format the list of requirements that have no quotation yet,
    with the top suggested vendor for each.

    unquoted items: req_name, req_id, top_vendor, top_score
    all_vendors: list of vendor dicts (for context count)
    """
    if not unquoted:
        return "✅  All requirements have at least one quotation received."

    lines = [
        f"UNQUOTED REQUIREMENTS — {len(unquoted)} requirement(s) awaiting quotation",
        "=" * 66,
        f"Total vendors in system: {len(all_vendors)}",
        "",
        f"  {'Requirement':<36} {'Best Vendor Match':<24} {'Score':>5}",
        f"  {'─'*36} {'─'*24} {'─'*5}",
    ]

    for r in unquoted:
        lines.append(
            f"  {r['req_name'][:36]:<36} {r['top_vendor'][:24]:<24} {r['top_score']:>4}%"
        )

    lines += [
        "",
        "ACTION RECOMMENDED:",
        "  Use get_requirement_with_vendor_context(<req_name>) + email_agent",
        "  to send RFQ emails to the suggested vendors above.",
    ]

    return "\n".join(lines)


def format_rfq_readiness(data: list[dict]) -> str:
    """
    Format the RFQ readiness report.

    data items:
      req_name, quoted_vendors[str], unquoted_vendors[str],
      best_match_vendor, best_match_score, status
    """
    if not data:
        return "No requirements found for RFQ readiness report."

    fully_covered = [r for r in data if r["status"] == "covered"]
    partially_covered = [r for r in data if r["status"] == "partial"]
    not_covered = [r for r in data if r["status"] == "none"]

    lines = [
        "RFQ READINESS REPORT",
        "=" * 68,
        f"  Requirements with quotations   : {len(fully_covered) + len(partially_covered)}",
        f"  Requirements without quotations: {len(not_covered)}",
        "",
    ]

    if fully_covered or partially_covered:
        lines += ["✅  REQUIREMENTS WITH QUOTATION(S):", "─" * 50]
        for r in fully_covered + partially_covered:
            quoted_str = ", ".join(r["quoted_vendors"][:3]) or "N/A"
            lines += [
                f"  📋 {r['req_name']}",
                f"     Quoted by : {quoted_str}",
                f"     Best match: {r['best_match_vendor']} ({r['best_match_score']}%)",
            ]

    if not_covered:
        lines += ["", "❌  REQUIREMENTS WITH NO QUOTATION YET:", "─" * 50]
        for r in not_covered:
            top_unquoted = ", ".join(r["unquoted_vendors"][:3]) or "N/A"
            lines += [
                f"  📋 {r['req_name']}",
                f"     Best vendor (unquoted): {r['best_match_vendor']} ({r['best_match_score']}%)",
                f"     Suggested RFQ targets  : {top_unquoted}",
            ]

    lines += [
        "",
        "Next step: Use get_requirement_with_vendor_context() + email_agent to send RFQs.",
    ]

    return "\n".join(lines)


def format_best_vendor_for_category(
    category: str,
    results: list[dict],
) -> str:
    """
    Format best vendor ranking for a given category.

    results items: vendor_name, score, matched_reqs[str], email, certs
    """
    if not results:
        return f"No vendors found matching category '{category}'."

    lines = [
        f"BEST VENDORS FOR CATEGORY: '{category.upper()}'",
        "=" * 60,
        f"Vendors evaluated: {len(results)}",
        "",
        f"  {'#':<4} {'Vendor':<28} {'Avg Score':>9} {'Matched Reqs':>12}",
        f"  {'─'*4} {'─'*28} {'─'*9} {'─'*12}",
    ]

    for i, r in enumerate(results, 1):
        lines.append(
            f"  {i:<4} {r['vendor_name'][:28]:<28} {r['score']:>8}%"
            f" {r['matched_reqs']:>12}"
        )

    lines += ["", "DETAILS", "─" * 60]
    for i, r in enumerate(results, 1):
        lines += [
            f"\n  #{i}  {r['vendor_name']}",
            f"       Score          : {r['score']}%",
            f"       Email          : {r['email']}",
            f"       Certifications : {r['certs']}",
            f"       Matched reqs   : {r['matched_reqs']}",
        ]

    return "\n".join(lines)
