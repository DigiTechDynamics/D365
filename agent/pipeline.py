"""End-to-end flow: capture -> match -> code -> validate -> route -> approve -> transfer.

The agent ("svc-ap-agent") only proposes. People approve, and every action is written to the audit log.
"""
import hashlib
import json
from pathlib import Path

from . import accounting, master as M, matching, routing, store, validation
from .extract import extract_invoice
from .models import HEADER_FIELDS, ExtractedInvoice

AGENT = "svc-ap-agent"
INBOX = store.ROOT / "inbox"


class NotAllowed(Exception):
    """Raised when a user tries an action the controls do not permit."""


# ---------------------------------------------------------------- capture

def process_inbox():
    done = {c["source_file"] for c in store.all_cases()}
    created = []
    for pdf in sorted(INBOX.glob("*.pdf")):
        if pdf.name not in done:
            created.append(process_file(pdf))
    return created


def process_file(pdf: Path):
    ext, meta = extract_invoice(pdf, M.policy()["model"])
    case = {
        "id": store.next_id(), "source_file": pdf.name,
        "sha256": hashlib.sha256(pdf.read_bytes()).hexdigest(),
        "received_at": store.now(), "extraction_meta": meta, "extraction": ext.model_dump(),
        "corrections": [], "coding_overrides": {}, "verified_fields": [], "ap_verified": False,
        "steps": [], "status": "new", "touchless": False, "transfer": None,
    }
    store.audit(case["id"], AGENT, "captured",
                f"Read {pdf.name} ({'reference extraction, demo mode' if meta['mode'] == 'demo' else meta['model']})",
                {"sha256": case["sha256"]})
    evaluate(case, AGENT)
    return case


# ---------------------------------------------------------------- evaluate

def evaluate(case, actor):
    pol = M.policy()
    ext = ExtractedInvoice.model_validate(case["extraction"])
    vendor, method, score = M.find_vendor(ext.vendor_name.value, ext.vendor_tax_id.value)
    case["header"] = {f: getattr(ext, f).value for f in HEADER_FIELDS}
    case["confidence"] = {f: getattr(ext, f).confidence for f in HEADER_FIELDS}
    case["vendor_account"] = vendor["account"] if vendor else None
    case["vendor_match"] = {"method": method, "score": score}
    case["invoice_type"] = matching.classify(ext)
    po = M.purchase_order(ext.po_number.value) if case["invoice_type"] == "po" else None
    case["matches"] = matching.match_po_lines(ext, po, pol) if po else []
    case["allocations"] = matching.allocate_charges(ext, case["matches"], po) if po else []
    case["accounting"] = accounting.build(ext, case["invoice_type"], vendor, po, case["matches"],
                                          case["allocations"], case["coding_overrides"])
    case["checks"] = validation.run(case, ext, vendor, po, store.all_cases())

    if case["status"] in ("rejected", "transferred"):
        store.save(case)
        return case

    if any(c["status"] == "fail" and c["blocking"] for c in case["checks"]):
        case["status"], case["steps"] = "blocked", []
        reasons = "; ".join(c["detail"] for c in case["checks"] if c["blocking"] and c["status"] == "fail")
        store.audit(case["id"], actor, "blocked", reasons)
        store.save(case)
        return case

    steps, notes = routing.build_steps(case, vendor, po, case["steps"])
    case["routing_notes"] = notes
    clean = all(c["status"] in ("pass", "info") for c in case["checks"])
    t = pol["touchless"]
    if not steps and clean and t["enabled"] and case["invoice_type"] in t["invoice_types"] \
            and (case["header"]["total"] or 0) <= t["max_amount"]:
        case["steps"], case["status"], case["touchless"] = [], "approved", True
        store.audit(case["id"], AGENT, "approved_touchless",
                    f"All checks passed, {case['invoice_type'].upper()} invoice under {t['max_amount']:,} - no human approval required by policy")
        store.save(case)
        if t.get("auto_transfer"):
            transfer(case["id"], AGENT)
        return store.load(case["id"])
    if not steps:
        uid = M.users_with_role("APSpecialist")[0]["id"]
        steps = [{"rule": "DEFAULT", "description": "No approval rule matched - AP reviews", "kind": "verify",
                  "assignee": uid, "why": "fallback", "status": "pending", "decided_by": None, "decided_at": None,
                  "comment": None}]
    case["steps"] = steps
    case["status"] = "approved" if all(s["status"] == "approved" for s in steps) else "pending"
    store.audit(case["id"], actor, "routed",
                " -> ".join(f"{s['kind']}:{s['assignee']} ({s['rule']})" for s in steps)
                + (f" | {'; '.join(notes)}" if notes else ""))
    store.save(case)
    return case


# ---------------------------------------------------------------- human actions

def current_step(case):
    return next((s for s in case["steps"] if s["status"] == "pending"), None)


def _require_ap(user_id):
    u = M.user(user_id)
    if not u or u["role"] != "APSpecialist":
        raise NotAllowed("Only an AP Specialist can do this.")


def correct_fields(case_id, user_id, values):
    case = store.load(case_id)
    _require_ap(user_id)
    if case["status"] not in ("pending",):
        raise NotAllowed("Corrections are only possible while the invoice is in review.")
    changed = []
    for field, raw in values.items():
        if field not in HEADER_FIELDS:
            continue
        old = case["extraction"][field]["value"]
        new = raw.strip() if isinstance(raw, str) else raw
        if field in ("subtotal", "tax_amount", "total"):
            new = float(str(new).replace(",", "")) if new not in ("", None) else None
        if new in ("",):
            new = None
        if new != old:
            case["extraction"][field] = {"value": new, "confidence": 1.0}
            case["corrections"].append({"field": field, "old": old, "new": new, "by": user_id, "at": store.now()})
            changed.append(f"{field}: {old} -> {new}")
        if field not in case["verified_fields"]:
            case["verified_fields"].append(field)
    store.audit(case_id, user_id, "fields_verified", "; ".join(changed) if changed else "Confirmed values as extracted")
    return evaluate(case, user_id)


def override_coding(case_id, user_id, line_idx, fields):
    case = store.load(case_id)
    step = current_step(case)
    u = M.user(user_id)
    if case["status"] != "pending" or not u or (u["role"] != "APSpecialist" and (not step or step["assignee"] != user_id)):
        raise NotAllowed("Only AP or the current approver can change coding while the invoice is in review.")
    before = next((l for l in case["accounting"]["lines"] if l["invoice_line"] == line_idx), {})
    clean = {k: (v or "").strip() for k, v in fields.items()}
    case["coding_overrides"][str(line_idx)] = clean
    store.audit(case_id, user_id, "coding_changed",
                f"line {line_idx + 1}: " + ", ".join(f"{k} {before.get(k)} -> {v or None}" for k, v in clean.items()
                                                     if (before.get(k) or "") != v))
    return evaluate(case, user_id)


def decide(case_id, user_id, decision, comment=""):
    case = store.load(case_id)
    step = current_step(case)
    if case["status"] == "blocked" and decision == "reject":
        _require_ap(user_id)
        case["status"] = "rejected"
        store.audit(case_id, user_id, "rejected", comment or "Rejected blocked invoice")
        store.save(case)
        return case
    if not step:
        raise NotAllowed("Nothing is waiting for approval on this invoice.")
    if step["assignee"] != user_id:
        raise NotAllowed(f"This step is assigned to {step['assignee']}. You are acting as {user_id}.")
    vendor = M.vendor(case["vendor_account"])
    conflicts = routing._sod_conflicts(vendor)
    if step["kind"] == "approve" and user_id in conflicts:
        raise NotAllowed(f"Segregation of duties: {user_id} {conflicts[user_id]}.")
    # Hierarchy approval: earlier approvers endorse; the final approver's limit must cover the full amount.
    total = case["header"]["total"] or 0
    last_approve = [s for s in case["steps"] if s["kind"] == "approve"][-1:] == [step]
    if decision == "approve" and last_approve and M.user(user_id)["approval_limit"] < total:
        raise NotAllowed(f"Approval limit {M.user(user_id)['approval_limit']:,} is below the invoice total {total:,.2f}, "
                         "and no higher approver follows in the route.")
    if decision == "approve" and step["kind"] == "verify":
        open_fails = [c for c in case["checks"] if c["status"] == "fail"]
        if open_fails:
            raise NotAllowed("Resolve the failed checks first: " + "; ".join(c["label"] for c in open_fails))
    step.update({"status": "approved" if decision == "approve" else "rejected", "decided_by": user_id,
                 "decided_at": store.now(), "comment": comment or None})
    store.audit(case_id, user_id, f"{step['kind']}_{step['status']}", comment or step["description"])
    if decision != "approve":
        case["status"] = "rejected"
        store.save(case)
        return case
    if step["kind"] == "verify":
        case["ap_verified"] = True
        for f in HEADER_FIELDS:
            if f not in case["verified_fields"] and case["confidence"][f] < M.policy()["confidence_threshold"]:
                case["verified_fields"].append(f)
        return evaluate(case, user_id)
    if all(s["status"] == "approved" for s in case["steps"]):
        case["status"] = "approved"
        store.audit(case_id, user_id, "fully_approved", "All approval steps complete")
    store.save(case)
    return case


# ---------------------------------------------------------------- transfer

def transfer(case_id, actor):
    case = store.load(case_id)
    if case["status"] != "approved":
        raise NotAllowed("Only approved invoices can be transferred.")
    if actor != AGENT:
        _require_ap(actor)
    target = M.policy()["transfer_targets"][case["invoice_type"]]
    h = case["header"]
    payload = {
        "_note": "Shape of what would be sent to a D365 F&O sandbox. Not sent anywhere in this demo.",
        "target": target,
        "dataAreaId": M.md()["legal_entity"],
        "header": {
            "VendorAccount": case["vendor_account"], "InvoiceNumber": h["invoice_number"],
            "InvoiceDate": h["invoice_date"], "DueDate": h["due_date"], "Currency": h["currency"],
            "PurchaseOrder": h["po_number"], "InvoiceAmount": h["total"], "SalesTaxAmount": h["tax_amount"],
            "CaptureReference": case["id"],
        },
        "lines": [{
            "LineNumber": i + 1, "Description": l["description"], "AccountType": l["account_type"],
            "MainAccount": l["main_account"], "Project": l["Project"], "ProjectCategory": l.get("project_category"),
            "Dimensions": {d: l[d] for d in ("BusinessUnit", "CostCenter", "Department") if l.get(d)},
            "Amount": l["amount"], "ItemSalesTaxGroup": l["item_tax_group"],
        } for i, l in enumerate(case["accounting"]["lines"])],
        "attachments": [{
            "file": case["source_file"], "sha256": case["sha256"], "attach_to": "header",
            "requirement": "Must stay retrievable from the posted voucher - check this in your D365 version.",
        }],
        "approvals": [{"step": s["description"], "by": s["decided_by"], "at": s["decided_at"]}
                      for s in case["steps"]] or [{"step": "Touchless per policy", "by": AGENT, "at": store.now()}],
    }
    store.OUTBOX.mkdir(exist_ok=True)
    out = store.OUTBOX / f"{case['id']}.json"
    out.write_text(json.dumps(payload, indent=2))
    case["status"] = "transferred"
    case["transfer"] = {"target": target, "file": out.name, "by": actor, "at": store.now()}
    store.audit(case_id, actor, "transferred", f"{target} payload written to outbox/{out.name}")
    store.save(case)
    return case


# ---------------------------------------------------------------- KPIs

def kpis():
    cases = store.all_cases()
    done = [c for c in cases if c["status"] == "transferred"]
    ftr = [c for c in done if not c["corrections"] and not c["coding_overrides"]]
    return {
        "total": len(cases),
        "touchless": sum(1 for c in cases if c["touchless"]),
        "awaiting": sum(1 for c in cases if c["status"] == "pending"),
        "blocked": sum(1 for c in cases if c["status"] == "blocked"),
        "transferred": len(done),
        "first_time_right_pct": round(100 * len(ftr) / len(done)) if done else None,
        "touchless_pct": round(100 * sum(1 for c in cases if c["touchless"]) / len(cases)) if cases else None,
    }
