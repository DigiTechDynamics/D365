"""Approval routing driven by config/policy.json, with segregation of duties and delegation."""
from datetime import datetime, timezone

from . import master as M, store


def overdue(step):
    """True when a pending step has waited longer than the escalation SLA."""
    if step["status"] != "pending" or not step.get("assigned_at"):
        return False
    waited = datetime.now(timezone.utc) - datetime.fromisoformat(step["assigned_at"])
    return waited.days >= M.policy()["escalation"]["sla_days"]


def _conditions(case, steps_so_far):
    total = case["header"].get("total") or 0
    checks = case["checks"]
    needs_ap = any(c["status"] == "fail" and not c["blocking"] for c in checks) or \
        any(c["id"] == "confidence" and c["status"] == "warn" for c in checks)
    exceeds = any(s["kind"] == "approve" and (M.user(s["assignee"]) or {}).get("approval_limit", 0) < total
                  for s in steps_so_far)
    return {
        "invoice_type": case["invoice_type"],
        "amount": total,
        "has_variance": any(c["id"] == "match" and c["status"] == "warn" for c in checks),
        "needs_ap_review": needs_ap and not case.get("ap_verified"),
        "exceeds_approver_limit": exceeds,
    }


def _rule_applies(when, cond):
    for key, want in when.items():
        if key == "invoice_type" and cond["invoice_type"] not in want:
            return False
        if key == "min_amount" and cond["amount"] <= want:
            return False
        if key == "max_amount" and cond["amount"] > want:
            return False
        if key in ("has_variance", "needs_ap_review", "exceeds_approver_limit") and cond[key] != want:
            return False
    return True


def _resolve(resolver, case, vendor, po):
    """Return (user_id, why) for a resolver name."""
    data = M.md()
    if resolver.startswith("role:"):
        role = resolver.split(":", 1)[1]
        users = M.users_with_role(role)
        return (users[0]["id"], f"role {role}") if users else (None, f"no user with role {role}")
    if resolver == "po_buyer" and po:
        return po["buyer"], f"buyer on PO {po['po']}"
    if resolver == "cost_center_owner":
        cc = next((l["CostCenter"] for l in case["accounting"]["lines"] if l.get("CostCenter")), None)
        owner = data["dimensions"]["CostCenter"].get(cc, {}).get("owner") if cc else None
        return owner, f"owner of cost center {cc}"
    if resolver == "project_manager":
        ref = case["header"].get("project_reference")
        pm = data["projects"].get(ref, {}).get("manager")
        return pm, f"manager of project {ref}"
    return None, f"cannot resolve {resolver}"


def _sod_conflicts(vendor):
    sod = M.policy()["segregation_of_duties"]
    blocked = {}
    if vendor and sod.get("vendor_requester_cannot_approve") and vendor.get("requested_by"):
        blocked[vendor["requested_by"]] = "requested this vendor's onboarding"
    if vendor and sod.get("vendor_creator_cannot_approve") and vendor.get("created_by"):
        blocked[vendor["created_by"]] = "created this vendor in master data"
    return blocked


def build_steps(case, vendor, po, previous_steps=()):
    pol = M.policy()
    conflicts = _sod_conflicts(vendor)
    steps, notes = [], []
    for rule in pol["approval_rules"]:
        cond = _conditions(case, steps)
        if not _rule_applies(rule["when"], cond):
            continue
        uid, why = _resolve(rule["step"]["resolver"], case, vendor, po)
        if uid is None:
            notes.append(f"{rule['id']}: {why}")
            continue
        if rule["step"]["kind"] == "approve" and uid in conflicts:
            alt = None
            if rule["step"]["resolver"] == "cost_center_owner":
                cc = next((l["CostCenter"] for l in case["accounting"]["lines"] if l.get("CostCenter")), None)
                alt = M.md()["dimensions"]["CostCenter"].get(cc, {}).get("deputy")
            alt = alt or M.users_with_role("FinanceController")[0]["id"]
            notes.append(f"Segregation of duties: {uid} {conflicts[uid]}, so {rule['id']} goes to {alt}")
            why += f" -> reassigned to {alt} (SoD: {uid} {conflicts[uid]})"
            uid = alt
        u = M.user(uid)
        if u and u.get("out_of_office") and u.get("delegate_to"):
            notes.append(f"Delegation: {uid} is out of office, delegated to {u['delegate_to']}")
            why += f" -> delegated to {u['delegate_to']} (out of office)"
            uid = u["delegate_to"]
        if pol["segregation_of_duties"].get("one_person_one_step") and \
                any(s["assignee"] == uid and s["kind"] == rule["step"]["kind"] for s in steps):
            notes.append(f"{rule['id']}: {uid} already approves this invoice, step not duplicated")
            continue
        steps.append({"rule": rule["id"], "description": rule["description"], "kind": rule["step"]["kind"],
                      "assignee": uid, "why": why, "status": "pending", "decided_by": None, "decided_at": None,
                      "comment": None, "assigned_at": store.now()})

    # Keep decisions (and waiting time) already recorded on identical steps, e.g. after AP corrections.
    for s in steps:
        prev = next((p for p in previous_steps if p["rule"] == s["rule"] and p["assignee"] == s["assignee"]), None)
        if prev:
            s["assigned_at"] = prev.get("assigned_at", s["assigned_at"])
            if prev["status"] == "approved":
                s.update({k: prev[k] for k in ("status", "decided_by", "decided_at", "comment")})
    # A verify step, once created, stays until AP explicitly confirms it - fixing a field never skips it.
    prev_verify = [p for p in previous_steps if p["kind"] == "verify" and p["status"] in ("approved", "pending")]
    if prev_verify and not any(s["kind"] == "verify" for s in steps):
        steps = prev_verify[:1] + steps
    return steps, notes
