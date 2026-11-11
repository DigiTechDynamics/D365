"""Proposed accounting: main account, financial dimensions, project and tax for every invoice line.

This answers "will it post correctly?", not just "did it match?". Every coding decision records its source.
"""
from collections import Counter, defaultdict

from . import master as M

DIMS = ["CostCenter", "Department", "BusinessUnit"]


def _keyword_rule(description):
    d = description.lower()
    for r in M.md()["coding_keywords"]:
        if any(k in d for k in r["keywords"]):
            return r
    return None


def vendor_history_coding(vendor_account):
    prior = [p for p in M.md()["posted_invoices"] if p["vendor"] == vendor_account]
    if not prior:
        return None, 0
    key, n = Counter(tuple(sorted(p["coding"].items())) for p in prior).most_common(1)[0]
    return dict(key), n


def build(ext, inv_type, vendor, po, matches, allocations, overrides):
    data = M.md()
    lines = []

    if inv_type == "po" and po:
        capitalized = defaultdict(float)
        for a in allocations:
            for s in a["split"]:
                capitalized[s["invoice_line"]] += s["amount"]
        for m in matches:
            il = ext.lines[m["invoice_line"]]
            if m["po_line"] is None:
                lines.append(_line(m["invoice_line"], il, None, {}, None, il.amount, "TAXABLE", il.amount,
                                   "Not matched to a PO line - needs manual coding"))
                continue
            pl = next(p for p in po["lines"] if p["line"] == m["po_line"])
            cat = data["procurement_categories"][pl["category"]]
            extra = round(capitalized.get(m["invoice_line"], 0), 2)
            src = f"Procurement category {pl['category']} from PO line {pl['line']}"
            if extra:
                src += f"; includes {extra:,.2f} capitalized freight"
            lines.append(_line(m["invoice_line"], il, cat["main_account"], {d: pl.get(d) for d in DIMS}, None,
                               round(il.amount + extra, 2), cat["item_tax_group"], il.amount, src))
        first = po["lines"][0]
        for a in allocations:
            if a["treatment"] == "expense":
                il = ext.lines[a["charge_line"]]
                cfg = data["charge_codes"][a["code"]]
                lines.append(_line(a["charge_line"], il, cfg["expense_account"], {d: first.get(d) for d in DIMS}, None,
                                   il.amount, cfg["item_tax_group"], il.amount, f"Charge code {a['code']} (expensed)"))

    elif inv_type == "project":
        ref = ext.project_reference.value
        proj = data["projects"].get(ref)
        for idx, il in enumerate(ext.lines):
            rule = _keyword_rule(il.description)
            acct = (rule or {}).get("main_account") or (proj or {}).get("main_account")
            dims = {d: (proj or {}).get(d) for d in DIMS}
            cat = (rule or {}).get("project_category", "Subcontract labor")
            src = f"Project {ref} defaults; project category '{cat}'" if proj else f"Project {ref} not found"
            ln = _line(idx, il, acct, dims, ref, il.amount, "TAXABLE", il.amount, src)
            ln["account_type"] = "Project"
            ln["project_category"] = cat
            lines.append(ln)

    else:  # cost / overhead invoice: no PO to inherit coding from
        coding, src = None, None
        if vendor and vendor.get("default_coding"):
            coding, src = vendor["default_coding"], "Vendor default coding"
        elif vendor:
            hist, n = vendor_history_coding(vendor["account"])
            if hist:
                coding, src = hist, f"Vendor posting history ({n} prior invoices)"
        for idx, il in enumerate(ext.lines):
            rule = _keyword_rule(il.description)
            acct = (rule or {}).get("main_account") or (coding or {}).get("main_account")
            line_src = src or "No default coding, history or rule found"
            if rule and rule.get("main_account"):
                line_src = f"Keyword rule ('{il.description.split(' - ')[0]}') -> {rule['main_account']}; dimensions from {src or 'none'}"
            lines.append(_line(idx, il, acct, {d: (coding or {}).get(d) for d in DIMS}, (coding or {}).get("Project"),
                               il.amount, "TAXABLE", il.amount, line_src))

    # Human corrections are applied last and flagged, so first-time-right can be measured.
    for ln in lines:
        ov = overrides.get(str(ln["invoice_line"]))
        if ov:
            for k, v in ov.items():
                if k in ("main_account", "Project") or k in DIMS:
                    ln[k] = v or None
            ln["overridden"] = True
            ln["source"] += " (corrected by user)"

    tg = data["tax_groups"].get((vendor or {}).get("tax_group"), {"rate_pct": 0, "applies_to": []})
    taxable = sum(ln["taxable_base"] for ln in lines if ln["item_tax_group"] in tg["applies_to"])
    expected_tax = round(taxable * tg["rate_pct"] / 100, 2)
    invoice_tax = ext.tax_amount.value or 0.0
    debit = round(sum(ln["amount"] for ln in lines) + invoice_tax, 2)
    credit = round(ext.total.value or 0.0, 2)
    return {
        "lines": lines,
        "tax_group": (vendor or {}).get("tax_group"),
        "tax_rate_pct": tg["rate_pct"],
        "taxable_base": round(taxable, 2),
        "expected_tax": expected_tax,
        "invoice_tax": invoice_tax,
        "debit_total": debit,
        "credit_total": credit,
        "balanced": abs(debit - credit) < 0.01,
    }


def _line(idx, il, acct, dims, project, amount, item_tax_group, taxable_base, source):
    return {
        "invoice_line": idx, "description": il.description, "account_type": "Ledger",
        "main_account": acct, **{d: dims.get(d) for d in DIMS}, "Project": project,
        "amount": round(amount, 2), "item_tax_group": item_tax_group, "taxable_base": round(taxable_base, 2),
        "source": source, "overridden": False,
    }
