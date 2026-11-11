"""Invoice type, PO line matching and charge handling."""
from . import master as M


def classify(ext):
    if ext.po_number.value:
        return "po"
    if ext.project_reference.value:
        return "project"
    return "cost"


def charge_code(description):
    d = description.lower()
    for code, c in M.md()["charge_codes"].items():
        if any(k in d for k in c["keywords"]):
            return code
    return "FREIGHT"


def match_po_lines(ext, po, pol):
    """Two/three-way match of goods lines against PO price and received quantity."""
    results = []
    used = set()
    for idx, line in enumerate(ext.lines):
        if line.is_charge:
            continue
        cand = None
        if line.item_code:
            cand = next((pl for pl in po["lines"] if pl["item"] == line.item_code and pl["line"] not in used), None)
        if not cand:
            scored = [(M.similarity(pl["description"], line.description), pl) for pl in po["lines"] if pl["line"] not in used]
            if scored:
                s, pl = max(scored, key=lambda t: t[0])
                cand = pl if s >= 0.6 else None
        if not cand:
            results.append({"invoice_line": idx, "po_line": None, "status": "unmatched",
                            "detail": "No PO line found for this invoice line"})
            continue
        used.add(cand["line"])
        price_var = ((line.unit_price or 0) - cand["unit_price"]) / cand["unit_price"] * 100 if cand["unit_price"] else 0
        qty = line.quantity or 0
        issues = []
        if abs(price_var) > pol["price_tolerance_pct"]:
            issues.append(f"price {line.unit_price:,.2f} vs PO {cand['unit_price']:,.2f} ({price_var:+.1f}%, tolerance {pol['price_tolerance_pct']}%)")
        if qty > cand["received_qty"] * (1 + pol["qty_tolerance_pct"] / 100):
            issues.append(f"quantity {qty:g} exceeds received {cand['received_qty']:g}")
        results.append({
            "invoice_line": idx, "po_line": cand["line"], "po_item": cand["item"],
            "status": "variance" if issues else "matched",
            "price_variance_pct": round(price_var, 2),
            "detail": "; ".join(issues) if issues else "Price and quantity within tolerance (PO + receipt)",
        })
    return results


def allocate_charges(ext, matches, po):
    """Capitalized charges are spread over matched goods lines pro-rata by amount."""
    allocations = []
    goods = [m for m in matches if m["po_line"] is not None]
    base = sum(ext.lines[m["invoice_line"]].amount for m in goods)
    for idx, line in enumerate(ext.lines):
        if not line.is_charge:
            continue
        code = charge_code(line.description)
        cfg = M.md()["charge_codes"][code]
        if cfg["treatment"] == "capitalize" and base > 0:
            split, remaining = [], round(line.amount, 2)
            for i, m in enumerate(goods):
                share = remaining if i == len(goods) - 1 else round(line.amount * ext.lines[m["invoice_line"]].amount / base, 2)
                remaining = round(remaining - share, 2) if i < len(goods) - 1 else 0
                split.append({"invoice_line": m["invoice_line"], "amount": share})
            allocations.append({"charge_line": idx, "code": code, "treatment": "capitalize", "method": "pro-rata by line amount",
                                "split": split})
        else:
            allocations.append({"charge_line": idx, "code": code, "treatment": "expense",
                                "method": f"expensed to {cfg['expense_account']}", "split": []})
    return allocations
