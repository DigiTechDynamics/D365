"""Review UI for the AP invoice agent.  Run:  python app.py   then open http://127.0.0.1:5000"""
import secrets
import shutil

from flask import Flask, abort, flash, redirect, render_template, request, send_from_directory, session, url_for

from agent import chasing, d365, intake, mail, master as M, pipeline, sources, store
from agent.extract import credentials_available
from agent.models import HEADER_FIELDS
from agent.validation import ACKNOWLEDGEABLE

app = Flask(__name__)
app.secret_key = secrets.token_hex(16)

FIELD_LABELS = {
    "vendor_name": "Vendor name", "vendor_tax_id": "Vendor tax ID", "invoice_number": "Invoice number",
    "invoice_date": "Invoice date", "due_date": "Due date", "currency": "Currency", "po_number": "Purchase order",
    "project_reference": "Project reference", "subtotal": "Subtotal", "tax_amount": "Sales tax", "total": "Invoice total",
}
STATUS_CHIP = {"pending": "warn", "approved": "ok", "transferred": "ok", "blocked": "bad", "rejected": "bad", "new": "info"}


@app.template_filter("money")
def money(v):
    return "" if v is None else f"{v:,.2f}"


@app.context_processor
def globals_():
    uid = session.get("user", "pchen")
    return {"me": M.user(uid), "users": M.md()["users"], "status_chip": STATUS_CHIP, "labels": FIELD_LABELS,
            "live": credentials_available(), "user_name": lambda u: (M.user(u) or {"name": u})["name"],
            "clock_days": store.clock_offset_days(), "today": store.now()[:10],
            "ap_mail_count": len(chasing.needs_ap())}


def me():
    return session.get("user", "pchen")


def _guard(fn):
    """Run a user action; show control violations as messages; then let the agent do its follow-ups."""
    try:
        fn()
    except pipeline.NotAllowed as e:
        flash(str(e), "bad")
    chasing.run()


@app.post("/act-as")
def act_as():
    if M.user(request.form["user"]):
        session["user"] = request.form["user"]
    return redirect(request.referrer or url_for("queue"))


@app.get("/")
def queue():
    cases = store.all_cases()
    runs = intake.registry()["runs"]
    return render_template("queue.html", cases=cases, k=pipeline.kpis(), sources=sources.config()["sources"],
                           last_run=runs[-1] if runs else None, chase={c["id"]: chasing.status(c) for c in cases})


@app.post("/process")
def process():
    try:
        summary, created = pipeline.run_all()
        chasing.run()
        flash(f"Checked sources: {len(summary['accepted'])} file(s) received, {len(summary['rejected'])} rejected. "
              f"Read {len(created)} new invoice(s).", "ok")
        for err in summary["errors"]:
            flash(f"Source error - {err}", "bad")
    except Exception as e:  # surfaced to the user rather than hidden
        flash(f"Processing failed: {e}", "bad")
    return redirect(url_for("queue"))


@app.post("/clock")
def clock():
    store.advance_clock(int(request.form.get("days", 1)))
    chasing.run()
    flash(f"Demo clock moved forward. Today is now {store.now()[:10]}; reminders and escalations were checked.", "ok")
    return redirect(request.referrer or url_for("queue"))


@app.post("/reset")
def reset():
    shutil.rmtree(store.STATE, ignore_errors=True)
    shutil.rmtree(store.OUTBOX, ignore_errors=True)
    for f in pipeline.INBOX.glob("*"):
        if f.is_file() and f.name != ".gitkeep":
            f.unlink()
    M.refresh()
    flash("Demo reset: cases, mail, audit log, clock, outbox, inbox and mock D365 cleared.", "ok")
    return redirect(url_for("queue"))


@app.get("/case/<cid>")
def case(cid):
    c = store.load(cid)
    if not c:
        abort(404)
    step = pipeline.current_step(c)
    u = M.user(me())
    can_fix = c["status"] == "pending" and (u["role"] == "APSpecialist")
    can_code = c["status"] == "pending" and (u["role"] == "APSpecialist" or (step and step["assignee"] == me()))
    data = M.md()
    return render_template(
        "case.html", c=c, step=step, can_fix=can_fix, can_code=can_code,
        vendor=M.vendor(c["vendor_account"]), po=M.purchase_order(c["header"].get("po_number")),
        audit=store.read_audit(cid), threshold=M.policy()["confidence_threshold"], fields=HEADER_FIELDS,
        accounts=data["main_accounts"], dims=data["dimensions"], projects=data["projects"],
        lines=c["extraction"]["lines"], match_by_line={m["invoice_line"]: m for m in c["matches"]},
        ackable=ACKNOWLEDGEABLE, src=c.get("source") or {"channel": "manual"},
        messages=mail.all_messages(cid), approver=M.user(step["assignee"]) if step else None,
    )


@app.post("/case/<cid>/clear-check")
def clear_check(cid):
    _guard(lambda: pipeline.acknowledge_check(cid, me(), request.form["check"], request.form.get("note", "")))
    return redirect(url_for("case", cid=cid))


@app.post("/case/<cid>/verify")
def verify(cid):
    values = {f: request.form[f] for f in HEADER_FIELDS if f in request.form}
    _guard(lambda: pipeline.correct_fields(cid, me(), values))
    return redirect(url_for("case", cid=cid))


@app.post("/case/<cid>/coding/<int:line>")
def coding(cid, line):
    fields = {k: request.form.get(k, "") for k in ("main_account", "CostCenter", "Department", "Project")}
    _guard(lambda: pipeline.override_coding(cid, me(), line, fields))
    return redirect(url_for("case", cid=cid))


@app.post("/case/<cid>/decide")
def decide(cid):
    _guard(lambda: pipeline.decide(cid, me(), request.form["decision"], request.form.get("comment", "")))
    return redirect(url_for("case", cid=cid))


@app.post("/case/<cid>/transfer")
def transfer(cid):
    _guard(lambda: pipeline.transfer(cid, me()))
    return redirect(url_for("case", cid=cid))


@app.post("/case/<cid>/reply")
def reply(cid):
    """Demo only: type the approver's email reply as if it had arrived in the AP mailbox."""
    _guard(lambda: chasing.receive_reply(cid, request.form["sender"], request.form["body"]))
    return redirect(url_for("case", cid=cid) + "#mail")


@app.post("/mail/<mid>/send")
def mail_send(mid):
    m = mail.get(mid)
    _guard(lambda: chasing.send_draft(mid, me(), request.form.get("subject"), request.form.get("body")))
    return redirect(request.referrer or url_for("case", cid=m["case"]))


@app.post("/mail/<mid>/discard")
def mail_discard(mid):
    _guard(lambda: chasing.discard_draft(mid, me()))
    return redirect(request.referrer or url_for("mailbox"))


@app.post("/mail/<mid>/close")
def mail_close(mid):
    _guard(lambda: chasing.resolve_followup(mid, me(), request.form.get("note", "")))
    return redirect(request.referrer or url_for("mailbox"))


@app.get("/mail")
def mailbox():
    msgs = list(reversed(mail.all_messages()))
    return render_template("mail.html", msgs=msgs, todo=[m for m in msgs if m["status"] in ("draft", "needs_ap")])


@app.get("/d365")
def d365_page():
    cfg = d365.config()
    return render_template("d365.html", cfg=cfg, records=d365.mock_records(), source=M.md().get("_source"),
                           check=d365.check_mapping() if d365.enabled() else [])


@app.get("/pdf/<path:name>")
def pdf(name):
    return send_from_directory(pipeline.INBOX, name)


@app.get("/audit")
def audit():
    return render_template("audit.html", rows=list(reversed(store.read_audit())))


@app.get("/policy")
def policy():
    return render_template("policy.html", p=M.policy())


if __name__ == "__main__":
    app.run(debug=False, port=5000)
