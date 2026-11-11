"""Validation checks. Each check is pass / warn / fail / info, and 'blocking' failures stop the invoice."""
from datetime import date

from . import master as M
from .models import HEADER_FIELDS


def check(cid, label, status, detail, blocking=False):
    return {"id": cid, "label": label, "status": status, "detail": detail, "blocking": blocking}


def run(case, ext, vendor, po, other_cases):
    pol, data = M.policy(), M.md()
    out = []
    verified = set(case.get("verified_fields", []))

    # Vendor
    if not vendor:
        out.append(check("vendor", "Vendor exists in Finance", "fail",
                         f"No vendor matches '{ext.vendor_name.value}' / tax ID {ext.vendor_tax_id.value}", True))
    else:
        out.append(check("vendor", "Vendor exists in Finance", "pass",
                         f"{vendor['account']} matched by {case['vendor_match']['method']}"))
        if vendor["status"] != "active":
            out.append(check("vendor_status", "Vendor is active and not on hold", "fail", f"Status: {vendor['status']}", True))

    # Where it came from: sender domain and payment-detail change requests (common invoice fraud patterns)
    src = case.get("source") or {}
    if src.get("channel") == "email":
        sender = src.get("sender") or ""
        domain = sender.split("@")[-1]
        if vendor and M.sources_config()["screening"].get("require_known_sender_domain"):
            known = vendor.get("email_domains", [])
            ok = domain in known
            out.append(check("sender", "Sent from the vendor's registered email domain", "pass" if ok else "fail",
                             f"{sender} {'matches' if ok else 'is NOT registered for'} {vendor['account']} "
                             f"(on file: {', '.join(known) or 'none'})"))
        text = f"{src.get('subject', '')} {src.get('body_excerpt', '')}".lower()
        hits = [p for p in M.sources_config()["screening"]["payment_change_phrases"] if p in text]
        if hits:
            out.append(check("payment_change", "No request to change payment details", "fail",
                             f"Email mentions: {', '.join(hits)}. Confirm with the vendor using contact details already "
                             "on file - never the ones in the email - before this invoice is paid."))
    elif src.get("channel") == "sharepoint":
        out.append(check("source", "Source", "info", f"Uploaded to {src.get('location')} by {src.get('uploaded_by') or 'unknown'}"))

    # Duplicate
    num = (ext.invoice_number.value or "").strip().upper()
    dup = None
    if vendor and num:
        dup = next((p for p in data["posted_invoices"]
                    if p["vendor"] == vendor["account"] and p["invoice_number"].upper() == num), None)
        dup_case = next((c for c in other_cases if c["id"] != case["id"] and c.get("vendor_account") == vendor["account"]
                         and (c["header"].get("invoice_number") or "").upper() == num and c["status"] != "rejected"), None)
        if dup:
            out.append(check("duplicate", "No duplicate invoice", "fail",
                             f"Already posted as voucher {dup['voucher']} on {dup['date']}", True))
        elif dup_case:
            out.append(check("duplicate", "No duplicate invoice", "fail", f"Same invoice already captured as {dup_case['id']}", True))
        else:
            out.append(check("duplicate", "No duplicate invoice", "pass", "Not found in posted invoices or open captures"))

    # Mandatory + confidence
    missing = [f for f in ("invoice_number", "invoice_date", "total") if getattr(ext, f).value in (None, "")]
    out.append(check("mandatory", "Mandatory fields present", "fail" if missing else "pass",
                     ("Missing: " + ", ".join(missing)) if missing else "Invoice number, date and total present"))
    low = [f for f in HEADER_FIELDS
           if getattr(ext, f).value not in (None, "") and getattr(ext, f).confidence < pol["confidence_threshold"] and f not in verified]
    out.append(check("confidence", f"Extraction confidence at or above {pol['confidence_threshold']:.0%}",
                     "warn" if low else "pass",
                     ("Needs verification: " + ", ".join(low)) if low else "All extracted fields above threshold"))

    # Arithmetic
    line_sum = round(sum(l.amount for l in ext.lines), 2)
    sub, tax, tot = ext.subtotal.value or 0, ext.tax_amount.value or 0, ext.total.value or 0
    ok = abs(line_sum - sub) <= pol["total_tolerance"] and abs(sub + tax - tot) <= pol["total_tolerance"]
    out.append(check("totals", "Totals reconcile (lines + tax = total)", "pass" if ok else "fail",
                     f"Lines {line_sum:,.2f}, subtotal {sub:,.2f}, tax {tax:,.2f}, total {tot:,.2f}"))

    # Currency and dates
    if vendor:
        cur = ext.currency.value
        out.append(check("currency", "Currency matches vendor", "pass" if cur == vendor["currency"] else "fail",
                         f"Invoice {cur}, vendor {vendor['currency']}"))
    try:
        inv_d = date.fromisoformat(ext.invoice_date.value)
        due_d = date.fromisoformat(ext.due_date.value) if ext.due_date.value else None
        problems = []
        if inv_d > date.today():
            problems.append("invoice date is in the future")
        if due_d and due_d < inv_d:
            problems.append("due date before invoice date")
        if due_d and vendor and (due_d - inv_d).days != vendor["payment_terms_days"]:
            problems.append(f"due date implies {(due_d - inv_d).days} days, vendor terms are {vendor['payment_terms_days']}")
        out.append(check("dates", "Dates are plausible", "warn" if problems else "pass",
                         "; ".join(problems) if problems else "Invoice and due date consistent with vendor terms"))
    except (TypeError, ValueError):
        out.append(check("dates", "Dates are plausible", "fail", "Invoice date could not be read"))

    # PO / project
    if case["invoice_type"] == "po":
        if not po:
            out.append(check("po", "Purchase order exists and is open", "fail", f"PO {ext.po_number.value} not found"))
        elif vendor and po["vendor"] != vendor["account"]:
            out.append(check("po", "Purchase order exists and is open", "fail", f"PO {po['po']} belongs to vendor {po['vendor']}"))
        elif po["status"] != "open":
            out.append(check("po", "Purchase order exists and is open", "fail", f"PO status {po['status']}"))
        else:
            out.append(check("po", "Purchase order exists and is open", "pass", f"PO {po['po']}, buyer {po['buyer']}"))
            var = [m for m in case["matches"] if m["status"] != "matched"]
            out.append(check("match", "Lines match PO price and received quantity", "warn" if var else "pass",
                             "; ".join(f"line {m['invoice_line'] + 1}: {m['detail']}" for m in var) if var
                             else f"{len(case['matches'])} of {len(case['matches'])} lines matched within tolerance"))
    if case["invoice_type"] == "project":
        ref = ext.project_reference.value
        proj = data["projects"].get(ref)
        if not proj:
            out.append(check("project", "Project exists and is in process", "fail", f"Project {ref} not found"))
        else:
            out.append(check("project", "Project exists and is in process",
                             "pass" if proj["status"] == "in_process" else "fail", f"{ref} {proj['name']} - {proj['status']}"))

    # Accounting outcome
    acc = case["accounting"]
    uncoded = [str(l["invoice_line"] + 1) for l in acc["lines"] if not l["main_account"]]
    out.append(check("coding", "Every line has a main account", "fail" if uncoded else "pass",
                     ("Uncoded lines: " + ", ".join(uncoded)) if uncoded else "All lines coded"))
    dim_issues = []
    for l in acc["lines"]:
        for rule in pol["dimension_rules"]:
            applies = (rule.get("account_prefix") and (l["main_account"] or "").startswith(rule["account_prefix"])) \
                      or (rule.get("account_type") and l["account_type"] == rule["account_type"])
            if applies:
                for dim in rule["requires"]:
                    if not l.get(dim):
                        dim_issues.append(f"line {l['invoice_line'] + 1}: {dim} required for {l['main_account'] or l['account_type']}")
        for dim in ("CostCenter", "Department", "BusinessUnit"):
            v = l.get(dim)
            if v and v not in data["dimensions"][dim]:
                dim_issues.append(f"line {l['invoice_line'] + 1}: {dim} {v} does not exist")
            elif v and dim == "CostCenter" and not data["dimensions"][dim][v].get("active", True):
                dim_issues.append(f"line {l['invoice_line'] + 1}: cost center {v} is closed")
        if l["main_account"] and l["main_account"] not in data["main_accounts"]:
            dim_issues.append(f"line {l['invoice_line'] + 1}: main account {l['main_account']} does not exist")
    out.append(check("dimensions", "Financial dimensions valid for each account", "fail" if dim_issues else "pass",
                     "; ".join(dim_issues) if dim_issues else "Required dimensions present and active"))
    tax_diff = round(acc["invoice_tax"] - acc["expected_tax"], 2)
    out.append(check("tax", "Sales tax matches tax setup", "pass" if abs(tax_diff) <= pol["tax_tolerance"] else "fail",
                     f"Invoice {acc['invoice_tax']:,.2f}, expected {acc['expected_tax']:,.2f} "
                     f"({acc['tax_group']} {acc['tax_rate_pct']}% on {acc['taxable_base']:,.2f})"))
    out.append(check("balanced", "Proposed entry balances (debits = AP credit)", "pass" if acc["balanced"] else "fail",
                     f"Debits {acc['debit_total']:,.2f}, credit {acc['credit_total']:,.2f}"))

    if ext.notes:
        out.append(check("notes", "Notes from the document", "info", ext.notes))

    # A person can clear selected checks with a recorded reason (e.g. "called vendor on number on file").
    for c in out:
        ack = case.get("acknowledged", {}).get(c["id"])
        if ack and c["status"] == "fail" and c["id"] in ACKNOWLEDGEABLE:
            c["status"] = "pass"
            c["detail"] += f" | Cleared by {ack['by']}: {ack['note']}"
    return out


ACKNOWLEDGEABLE = {"sender", "payment_change"}
