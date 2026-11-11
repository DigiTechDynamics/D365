"""Approval chasing: request emails, reminders, escalation up the manager chain, and email replies.

Rules come from policy.json -> approval_chasing. The agent drafts; AP reviews requests before they go out
(configurable). An emailed "approved" counts only after the same controls as the Approve button: it must come
from the current approver's address, and approval limits and segregation of duties still apply.
"""
import re

from . import mail, master as M, pipeline, routing, store

AGENT = pipeline.AGENT
AP_MAILBOX = "ap-invoices@contoso.example"


def cfg():
    return M.policy()["approval_chasing"]


def step_key(step):
    return f"{step['rule']}:{step['assignee']}"


def _step_msgs(case_id, step):
    return [m for m in mail.all_messages(case_id) if m.get("step_key") == step_key(step)]


def _user_by_email(addr):
    return next((u for u in M.md()["users"] if u.get("email", "").lower() == (addr or "").lower()), None)


def _summary(case, step):
    h, acc = case["header"], case["accounting"]
    issues = [c for c in case["checks"] if c["status"] in ("warn", "fail")]
    coding = "\n".join(f"  - {l['description']}: {l['amount']:,.2f} to {l['main_account']}"
                       f"{' / ' + l['CostCenter'] if l.get('CostCenter') else ''}{' / ' + l['Project'] if l.get('Project') else ''}"
                       for l in acc["lines"])
    done = [s for s in case["steps"] if s["status"] == "approved"]
    return (
        f"Vendor: {h['vendor_name']} ({case['vendor_account']})\n"
        f"Invoice: {h['invoice_number']} dated {h['invoice_date']}, due {h['due_date']}\n"
        f"Amount: {h['total']:,.2f} {h['currency']}\n"
        f"Type: {_type_label(case)}\n"
        f"Why you: {step['description']} ({step['why']})\n\n"
        f"Proposed coding:\n{coding}\n\n"
        + (("Points to note:\n" + "\n".join(f"  - {c['label']}: {c['detail']}" for c in issues) + "\n\n") if issues else "")
        + (("Already approved by: " + ", ".join(M.user(s['decided_by'])['name'] for s in done) + "\n\n") if done else "")
    )


def _type_label(case):
    return {"po": "PO invoice", "cost": "Overhead (no PO)", "project": "Project invoice"}[case["invoice_type"]]


def _subject(case, prefix):
    h = case["header"]
    return f"[{case['id']}] {prefix}: {h['vendor_name']} {h['invoice_number']} - {h['total']:,.2f} {h['currency']}"


def _new_message(case, step, kind, subject, body):
    u = M.user(step["assignee"])
    status = "draft" if kind in cfg()["review_before_sending"] else "sent"
    msg = mail.add(case=case["id"], kind=kind, direction="out", step_key=step_key(step), to=u["email"],
                   to_user=u["id"], sender=AP_MAILBOX, subject=subject, body=body, status=status)
    if status == "sent":
        mail.update(msg["id"], sent_at=store.now(), sent_by=AGENT)
        store.audit(case["id"], AGENT, f"{kind}_sent", f"{msg['id']} to {u['name']}: {subject}")
    else:
        store.audit(case["id"], AGENT, f"{kind}_drafted", f"{msg['id']} to {u['name']} - waiting for AP review before sending")
    return msg


def _reply_instructions():
    return ("To decide, reply to this email with APPROVED or REJECTED and a short reason.\n"
            "Replies are only accepted from your own address and are checked against your approval limit.\n\n"
            "Accounts Payable (sent by the AP invoice agent)")


def draft_request(case, step):
    u = M.user(step["assignee"])
    body = (f"Hello {u['name'].split()[0]},\n\nPlease review this invoice for approval.\n\n"
            + _summary(case, step)
            + _reply_instructions())
    return _new_message(case, step, "request", _subject(case, "Approval needed"), body)


def _send_reminder(case, step, days):
    u = M.user(step["assignee"])
    body = (f"Hello {u['name'].split()[0]},\n\nA reminder that this invoice has been waiting for your approval for "
            f"{days} days. If there is no response it will be escalated to your manager.\n\n"
            + _summary(case, step)
            + _reply_instructions())
    return _new_message(case, step, "reminder", _subject(case, "Reminder - approval needed"), body)


def _escalate(case, step, days):
    vendor = M.vendor(case["vendor_account"])
    conflicts = routing._sod_conflicts(vendor)
    others = {s["assignee"] for s in case["steps"] if s is not step and s["status"] == "pending"}
    old = step["assignee"]
    cand = (M.user(old) or {}).get("manager")
    skipped = []
    while cand and (cand in conflicts or cand in others):
        skipped.append(cand)
        cand = (M.user(cand) or {}).get("manager")
    if not cand:
        return None
    note = f" -> escalated to {cand} after {days} days without a response"
    if skipped:
        note += f" (skipped {', '.join(skipped)}: already in the route or blocked by SoD)"
    step.update({"assignee": cand, "escalated_from": old, "assigned_at": store.now(), "why": step["why"] + note})
    store.save(case)
    store.audit(case["id"], AGENT, "escalated", f"Step {step['rule']} moved from {old} to {cand} after {days} days{'; skipped ' + ', '.join(skipped) if skipped else ''}")
    u = M.user(cand)
    body = (f"Hello {u['name'].split()[0]},\n\nThis invoice has been escalated to you because {M.user(old)['name']} "
            f"has not responded for {days} days. Please review it for approval.\n\n"
            + _summary(case, step)
            + _reply_instructions())
    return _new_message(case, step, "escalation", _subject(case, "Escalated - approval needed"), body)


# ------------------------------------------------------------------ human actions on messages

def send_draft(msg_id, user_id, subject=None, body=None):
    m = mail.get(msg_id)
    u = M.user(user_id)
    if not m or m["status"] != "draft":
        raise pipeline.NotAllowed("Only drafts can be sent.")
    if not u or u["role"] != "APSpecialist":
        raise pipeline.NotAllowed("Only AP can review and send approval emails.")
    edited = (subject and subject != m["subject"]) or (body and body.replace("\r\n", "\n") != m["body"])
    mail.update(msg_id, subject=subject or m["subject"], body=(body or m["body"]).replace("\r\n", "\n"),
                status="sent", sent_at=store.now(), sent_by=user_id)
    store.audit(m["case"], user_id, f"{m['kind']}_sent", f"{msg_id} to {m['to']}{' (edited by AP)' if edited else ''}")


def discard_draft(msg_id, user_id):
    m = mail.get(msg_id)
    u = M.user(user_id)
    if not m or m["status"] != "draft" or not u or u["role"] != "APSpecialist":
        raise pipeline.NotAllowed("Only AP can discard a draft.")
    mail.update(msg_id, status="discarded")
    store.audit(m["case"], user_id, "draft_discarded", msg_id)


def receive_reply(case_id, from_email, text):
    """Put an incoming reply in the inbox (demo: typed on the case page; live: from the AP mailbox)."""
    case = store.load(case_id)
    if not case:
        raise pipeline.NotAllowed("Unknown invoice.")
    last = next((m for m in reversed(mail.all_messages(case_id)) if m["direction"] == "out" and m["status"] == "sent"), None)
    msg = mail.add(case=case_id, kind="reply", direction="in", step_key=None, to=AP_MAILBOX, sender=from_email.strip().lower(),
                   subject="RE: " + (last["subject"] if last else f"[{case_id}]"), body=text, status="received",
                   in_reply_to=last["id"] if last else None)
    mail.update(msg["id"], sent_at=store.now())
    store.audit(case_id, "mailbox", "reply_received", f"{msg['id']} from {from_email}")
    return msg


def case_from_subject(subject):
    m = re.search(r"\[(CI-\d{6})\]", subject or "")
    return m.group(1) if m else None


def _process_reply(msg):
    case = store.load(msg["case"])
    interp = mail.interpret(msg["body"], M.policy()["model"])
    step = pipeline.current_step(case) if case else None
    sender = _user_by_email(msg["sender"])
    outcome, status = None, "needs_ap"
    if not case or case["status"] != "pending" or not step:
        outcome = "Invoice is no longer waiting for approval - no action taken"
        status = "processed"
    elif not sender or sender["id"] != step["assignee"]:
        outcome = (f"Ignored: sent by {msg['sender']}, but the current approver is {M.user(step['assignee'])['name']}. "
                   "AP should check this reply.")
    elif interp["decision"] in ("approve", "reject") and interp["confidence"] >= cfg()["reply_min_confidence"]:
        try:
            pipeline.decide(case["id"], sender["id"], interp["decision"],
                            f"By email reply {msg['id']}: {interp['reason'] or interp['decision']}")
            outcome, status = f"Recorded as {interp['decision']} by {sender['name']}", "processed"
        except pipeline.NotAllowed as e:
            outcome = f"Not applied: {e}"
    elif interp["decision"] == "question":
        outcome = f"{sender['name']} asked a question - AP to answer: {interp['reason']}"
    else:
        outcome = f"Reply is not a clear approve or reject ({interp['decision']}, confidence {interp['confidence']:.0%}) - AP to follow up"
    mail.update(msg["id"], interpretation=interp, outcome=outcome, status=status)
    store.audit(msg["case"], AGENT, "reply_read", f"{msg['id']}: {interp['decision']} ({interp['method']}) -> {outcome}")


def resolve_followup(msg_id, user_id, note):
    m = mail.get(msg_id)
    u = M.user(user_id)
    if not m or m["status"] != "needs_ap" or not u or u["role"] != "APSpecialist":
        raise pipeline.NotAllowed("Only AP can close a reply follow-up.")
    mail.update(msg_id, status="processed", outcome=(m.get("outcome") or "") + f" | Closed by {user_id}: {note}")
    store.audit(m["case"], user_id, "reply_followup_closed", f"{msg_id}: {note}")


# ------------------------------------------------------------------ the follow-up run

def run():
    """Read new replies, then draft / remind / escalate for every invoice waiting on an approver."""
    for m in mail.all_messages():
        if m["direction"] == "in" and m["status"] == "received":
            _process_reply(m)
    c = cfg()
    for case in store.all_cases():
        if case["status"] != "pending":
            continue
        step = pipeline.current_step(case)
        if not step or step["kind"] != "approve":
            continue  # AP verification steps are worked in the AP queue, not chased by email
        msgs = _step_msgs(case["id"], step)
        request = next((m for m in reversed(msgs) if m["kind"] in ("request", "escalation") and m["status"] != "discarded"), None)
        if not request:
            draft_request(case, step)
            continue
        if request["status"] != "sent":
            continue  # waiting for AP to review the draft
        days = int(store.days_since(request["sent_at"]))
        if days >= c["escalate_after_days"]:
            _escalate(case, step, days)
        elif days >= c["reminder_after_days"] and not any(m["kind"] == "reminder" for m in msgs):
            _send_reminder(case, step, days)


def status(case):
    """One-line chasing status for the queue."""
    step = pipeline.current_step(case)
    if case["status"] != "pending" or not step or step["kind"] != "approve":
        return None
    msgs = _step_msgs(case["id"], step)
    if any(m["status"] == "draft" for m in msgs):
        return ("warn", "Approval email drafted - AP to review and send")
    sent = [m for m in msgs if m["status"] == "sent"]
    if not sent:
        return None
    last = sent[-1]
    days = int(store.days_since(last["sent_at"]))
    label = {"request": "Requested", "reminder": "Reminded", "escalation": "Escalated"}[last["kind"]]
    return ("bad" if last["kind"] == "escalation" else "info", f"{label} {days} day(s) ago")


def needs_ap(case_id=None):
    return [m for m in mail.all_messages(case_id) if m["status"] in ("needs_ap", "draft")]
