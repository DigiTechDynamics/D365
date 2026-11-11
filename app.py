"""Review UI for the AP invoice agent.  Run:  python app.py   then open http://127.0.0.1:5000"""
import secrets
import shutil

from flask import Flask, abort, flash, redirect, render_template, request, send_from_directory, session, url_for

from agent import master as M, pipeline, routing, store
from agent.extract import credentials_available
from agent.models import HEADER_FIELDS

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
            "live": credentials_available(), "user_name": lambda u: (M.user(u) or {"name": u})["name"]}


def me():
    return session.get("user", "pchen")


@app.post("/act-as")
def act_as():
    if M.user(request.form["user"]):
        session["user"] = request.form["user"]
    return redirect(request.referrer or url_for("queue"))


@app.get("/")
def queue():
    cases = store.all_cases()
    esc = M.policy()["escalation"]["escalate_to"]
    target = M.users_with_role(esc.split(":", 1)[1])[0]["name"] if esc.startswith("role:") else esc
    overdue = {c["id"]: target for c in cases
               if (s := pipeline.current_step(c)) and routing.overdue(s)}
    return render_template("queue.html", cases=cases, k=pipeline.kpis(), overdue=overdue,
                           sla=M.policy()["escalation"]["sla_days"],
                           inbox=sorted(p.name for p in pipeline.INBOX.glob("*.pdf")))


@app.post("/process")
def process():
    try:
        created = pipeline.process_inbox()
        flash(f"Processed {len(created)} new invoice(s)." if created else "No new invoices in the inbox.", "ok")
    except Exception as e:  # surfaced to the user rather than hidden
        flash(f"Processing failed: {e}", "bad")
    return redirect(url_for("queue"))


@app.post("/reset")
def reset():
    shutil.rmtree(store.STATE, ignore_errors=True)
    shutil.rmtree(store.OUTBOX, ignore_errors=True)
    flash("Demo reset: cases, audit log and outbox cleared.", "ok")
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
    )


def _guard(fn):
    try:
        fn()
    except pipeline.NotAllowed as e:
        flash(str(e), "bad")


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
