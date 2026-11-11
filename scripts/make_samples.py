"""Generate fictional sample vendor invoices (PDF), reference extractions for demo mode, and the demo
mailbox (.eml files) and SharePoint folder that stand in for Microsoft 365.

All companies, people, domains and numbers are made up. Run:  python scripts/make_samples.py
"""
import json
import shutil
from email.message import EmailMessage
from pathlib import Path

from fpdf import FPDF

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "samples"
TRUTH = ROOT / "data" / "demo_extractions"
MAILBOX = ROOT / "demo_sources" / "mailbox"
SHAREPOINT = ROOT / "demo_sources" / "sharepoint" / "AP" / "Incoming"
AP_MAILBOX = "ap-invoices@contoso.example"

BILL_TO = ["Contoso Entertainment System USA (USMF)", "Accounts Payable Dept.", "100 Contoso Blvd, Redmond, WA 98052"]

INVOICES = [
    {
        "file": "FIS-77821.pdf",
        "vendor": ("Fabrikam Industrial Supply", "4410 Harbor Way, Tacoma, WA 98421", "91-4410276"),
        "number": "FIS-77821", "date": "2026-10-28", "due": "2026-11-27", "po": "00001942", "project": None,
        "lines": [("M0021", "Pallet racking kit - M2", 40, 185.00, False),
                  ("M0034", "Steel shelf panels", 120, 22.50, False),
                  ("M0040", "Mounting hardware set", 40, 14.75, False),
                  (None, "Freight & handling", 1, 640.00, True)],
        "tax": 962.10, "terms": "Net 30",
        "low_conf": {},
    },
    {
        "file": "PW-5530.pdf",
        "vendor": ("Proseware Packaging Co.", "77 Industrial Pkwy, Kent, WA 98032", "84-2219034"),
        "number": "PW-5530", "date": "2026-11-02", "due": "2026-12-02", "po": "00001977", "project": None,
        "lines": [("P-1001", "Corrugated shipping boxes 18x12x12", 2000, 0.95, False),
                  ("P-1002", "Stretch pallet wrap 18in", 50, 19.00, False)],
        "tax": 256.50, "terms": "Net 30",
        "low_conf": {},
    },
    {
        "file": "NW-2026-1102.pdf",
        "vendor": ("Northwind Facility Services", "1200 Pine St, Seattle, WA 98101", "47-8812350"),
        "number": "NW-2026-1102", "date": "2026-11-01", "due": "2026-11-16", "po": None, "project": None,
        "lines": [(None, "Monthly janitorial services - October 2026", 1, 3200.00, False),
                  (None, "Cleaning supplies and consumables", 1, 180.00, False)],
        "tax": 0.0, "terms": "Net 15",
        "low_conf": {"due_date": 0.72},
        "faint_due": True,
    },
    {
        "file": "ADT-4471.pdf",
        "vendor": ("Adatum IT Consulting", "500 Tech Center Dr, Bellevue, WA 98004", "26-5530918"),
        "number": "ADT-4471", "date": "2026-10-31", "due": "2026-11-30", "po": None, "project": None,
        "lines": [(None, "IT consulting services - ERP integration (hours)", 92, 195.00, False),
                  (None, "Travel expenses - client site visits", 1, 560.00, False)],
        "tax": 0.0, "terms": "Net 30",
        "low_conf": {},
    },
    {
        "file": "NW-2026-1088-resent.pdf",
        "vendor": ("Northwind Facility Services", "1200 Pine St, Seattle, WA 98101", "47-8812350"),
        "number": "NW-2026-1088", "date": "2026-10-03", "due": "2026-10-18", "po": None, "project": None,
        "lines": [(None, "Monthly janitorial services - September 2026", 1, 3200.00, False),
                  (None, "Cleaning supplies and consumables", 1, 180.00, False)],
        "tax": 0.0, "terms": "Net 15", "banner": "COPY - SECOND NOTICE",
        "low_conf": {},
    },
    {
        "file": "LIT-0931.pdf",
        "vendor": ("Litware Engineering Subcontractors", "88 Foundry Rd, Everett, WA 98201", "33-7781204"),
        "number": "LIT-0931", "date": "2026-11-05", "due": "2026-12-05", "po": None, "project": "PRJ-00045",
        "lines": [(None, "Structural engineering services (hours)", 120, 140.00, False),
                  (None, "Equipment rental - 3D laser scanner", 1, 1450.00, False)],
        "tax": 0.0, "terms": "Net 30",
        "low_conf": {},
    },
]


def money(v: float) -> str:
    return f"{v:,.2f}"


def render(inv: dict) -> None:
    name, addr, tax_id = inv["vendor"]
    pdf = FPDF(format="Letter")
    pdf.add_page()
    pdf.set_auto_page_break(False)

    pdf.set_font("Helvetica", "B", 22)
    pdf.set_text_color(27, 58, 92)
    pdf.cell(0, 12, "INVOICE", new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(40, 40, 40)
    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(0, 6, name, new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 9)
    pdf.cell(0, 5, addr, new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 5, f"Tax ID: {tax_id}", new_x="LMARGIN", new_y="NEXT")

    if inv.get("banner"):
        pdf.set_xy(120, 12)
        pdf.set_font("Helvetica", "B", 12)
        pdf.set_text_color(190, 30, 30)
        pdf.cell(80, 8, inv["banner"], align="R")
        pdf.set_text_color(40, 40, 40)

    meta = [("Invoice no:", inv["number"]), ("Invoice date:", inv["date"])]
    if inv["po"]:
        meta.append(("PO number:", inv["po"]))
    if inv["project"]:
        meta.append(("Your project ref:", inv["project"]))
    y = 24
    pdf.set_font("Helvetica", "", 10)
    for label, val in meta:
        pdf.set_xy(120, y)
        pdf.cell(40, 6, label, align="R")
        pdf.set_font("Helvetica", "B", 10)
        pdf.cell(40, 6, val, align="R")
        pdf.set_font("Helvetica", "", 10)
        y += 6
    pdf.set_xy(120, y)
    pdf.cell(40, 6, "Due date:", align="R")
    if inv.get("faint_due"):
        pdf.set_text_color(165, 165, 165)
        pdf.set_font("Times", "I", 10)
    else:
        pdf.set_font("Helvetica", "B", 10)
    pdf.cell(40, 6, inv["due"], align="R")
    pdf.set_text_color(40, 40, 40)

    pdf.set_xy(10, 62)
    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(0, 6, "Bill to", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 9)
    for l in BILL_TO:
        pdf.cell(0, 5, l, new_x="LMARGIN", new_y="NEXT")

    pdf.set_xy(10, 92)
    pdf.set_font("Helvetica", "B", 9)
    pdf.set_fill_color(235, 238, 242)
    cols = [(22, "Item"), (88, "Description"), (20, "Qty"), (28, "Unit price"), (32, "Amount")]
    for w, t in cols:
        pdf.cell(w, 7, t, border="B", fill=True, align="L" if t in ("Item", "Description") else "R")
    pdf.ln()
    pdf.set_font("Helvetica", "", 9)
    subtotal = 0.0
    for item, desc, qty, price, _charge in inv["lines"]:
        amt = round(qty * price, 2)
        subtotal += amt
        pdf.cell(22, 7, item or "", border="B")
        pdf.cell(88, 7, desc, border="B")
        pdf.cell(20, 7, f"{qty:g}", border="B", align="R")
        pdf.cell(28, 7, money(price), border="B", align="R")
        pdf.cell(32, 7, money(amt), border="B", align="R")
        pdf.ln()
    subtotal = round(subtotal, 2)
    total = round(subtotal + inv["tax"], 2)
    for label, val, bold in [("Subtotal", subtotal, False), ("Sales tax", inv["tax"], False), ("Total USD", total, True)]:
        pdf.set_font("Helvetica", "B" if bold else "", 10)
        pdf.cell(158, 7, label, align="R")
        pdf.cell(32, 7, money(val), align="R")
        pdf.ln()

    pdf.ln(8)
    pdf.set_font("Helvetica", "", 9)
    pdf.cell(0, 5, f"Payment terms: {inv['terms']}", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 5, f"Remit to: {name} - account ending 4821", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(6)
    pdf.set_font("Helvetica", "I", 9)
    pdf.cell(0, 5, "Thank you for your business.", align="C")

    SAMPLES.mkdir(parents=True, exist_ok=True)
    pdf.output(str(SAMPLES / inv["file"]))

    # Reference extraction used in demo mode (no API key). Mirrors agent/models.py.
    def tf(field, value, default=0.98):
        return {"value": value, "confidence": inv["low_conf"].get(field, default)}

    truth = {
        "vendor_name": tf("vendor_name", name, 0.99),
        "vendor_tax_id": tf("vendor_tax_id", tax_id, 0.97),
        "invoice_number": tf("invoice_number", inv["number"], 0.99),
        "invoice_date": tf("invoice_date", inv["date"]),
        "due_date": tf("due_date", inv["due"], 0.96),
        "currency": tf("currency", "USD", 0.99),
        "po_number": tf("po_number", inv["po"], 0.97),
        "project_reference": tf("project_reference", inv["project"], 0.95),
        "subtotal": {"value": subtotal, "confidence": 0.98},
        "tax_amount": {"value": inv["tax"], "confidence": 0.97},
        "total": {"value": total, "confidence": 0.99},
        "lines": [
            {"item_code": item, "description": desc, "quantity": qty, "unit_price": price,
             "amount": round(qty * price, 2), "is_charge": charge, "confidence": 0.97}
            for item, desc, qty, price, charge in inv["lines"]
        ],
        "notes": inv.get("banner"),
    }
    TRUTH.mkdir(parents=True, exist_ok=True)
    (TRUTH / (Path(inv["file"]).stem + ".json")).write_text(json.dumps(truth, indent=2))


EMAILS = [
    # (file, from, subject, date, body, attachments)
    ("01-fabrikam.eml", "Fabrikam Billing <billing@fabrikam-supply.example>", "Invoice FIS-77821 - PO 00001942",
     "Wed, 28 Oct 2026 09:14:00 -0700", "Hello,\n\nPlease find attached invoice FIS-77821 for PO 00001942.\n\nRegards,\nFabrikam Billing",
     ["FIS-77821.pdf"]),
    ("02-proseware.eml", "Proseware AR <ar@proseware.example>", "Invoice PW-5530",
     "Mon, 02 Nov 2026 11:02:00 -0800", "Hi AP team,\n\nInvoice PW-5530 attached. Note our box prices increased this quarter.\n\nThanks,\nProseware Accounts Receivable",
     ["PW-5530.pdf"]),
    ("03-northwind.eml", "Northwind Accounts <accounts@northwind-fs.example>", "October cleaning invoice NW-2026-1102",
     "Sun, 01 Nov 2026 16:40:00 -0800", "Good afternoon,\n\nAttached is our invoice for October janitorial services.\n\nNorthwind Facility Services",
     ["NW-2026-1102.pdf"]),
    ("04-northwind-reminder.eml", "Northwind Accounts <accounts@northwind-fs.example>", "SECOND NOTICE: invoice NW-2026-1088",
     "Tue, 03 Nov 2026 08:05:00 -0800", "Our records show invoice NW-2026-1088 as unpaid. A copy is attached.\n\nNorthwind Facility Services",
     ["NW-2026-1088-resent.pdf"]),
    ("05-adatum.eml", "Adatum Accounts <accounts@adatum-consu1ting.example>", "URGENT: invoice ADT-4471 and updated bank details",
     "Tue, 03 Nov 2026 07:58:00 -0800",
     "Dear customer,\n\nPlease find invoice ADT-4471 attached. Please note we have changed our bank details. "
     "Kindly remit payment to the new account below as this is an urgent payment.\n\nAccount number: XXXX-0000-DEMO\n\n"
     "Regards,\nAdatum Accounts", ["ADT-4471.pdf"]),
    ("06-unknown.eml", "Accounts Dept <payables@quick-invoice-portal.example>", "Overdue invoice - action required",
     "Wed, 04 Nov 2026 02:11:00 -0800", "Your invoice is overdue. Open the attached document to view and pay.",
     [("Invoice_8812.html", b"<html><body><h1>Invoice</h1><p>Sign in to view your invoice.</p></body></html>")]),
    ("07-fabrikam-reminder.eml", "Fabrikam Billing <billing@fabrikam-supply.example>", "Reminder: invoice FIS-77821",
     "Thu, 05 Nov 2026 09:30:00 -0800", "Friendly reminder - invoice FIS-77821 attached again for convenience.",
     ["FIS-77821.pdf"]),
    ("08-proseware-question.eml", "Proseware AR <ar@proseware.example>", "Question about PO 00001977",
     "Thu, 05 Nov 2026 10:00:00 -0800", "Hi, could you confirm the delivery address for PO 00001977? Thanks.", []),
]


def build_demo_sources():
    shutil.rmtree(ROOT / "demo_sources", ignore_errors=True)
    MAILBOX.mkdir(parents=True)
    SHAREPOINT.mkdir(parents=True)
    for i, (fname, sender, subject, date, body, atts) in enumerate(EMAILS, 1):
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"], msg["Date"] = sender, AP_MAILBOX, subject, date
        msg["Message-ID"] = f"<demo-{i:03d}@mail.contoso.example>"
        msg.set_content(body)
        for a in atts:
            if isinstance(a, tuple):
                msg.add_attachment(a[1], maintype="text", subtype="html", filename=a[0])
            else:
                msg.add_attachment((SAMPLES / a).read_bytes(), maintype="application", subtype="pdf", filename=a)
        (MAILBOX / fname).write_bytes(bytes(msg))
    # The project team uploads subcontractor invoices to SharePoint.
    shutil.copy(SAMPLES / "LIT-0931.pdf", SHAREPOINT / "LIT-0931.pdf")


if __name__ == "__main__":
    for inv in INVOICES:
        render(inv)
        print("created", inv["file"])
    build_demo_sources()
    print("demo mailbox:", len(EMAILS), "emails; SharePoint folder: 1 file")
