"""Dynamics 365 Finance & Operations connector (OData).

  - live: your D365 F&O sandbox. App-only Entra ID sign-in (client credentials) as a service user whose security
          role can READ master data and CREATE pending vendor invoices / invoice journals - but not post or approve.
  - mock: an in-memory stand-in seeded from data/masterdata.json and saved in state/mock_d365.json, so the
          integration runs end to end without a sandbox. It answers the same entity URLs the live client calls.

All entity and field names come from config/d365.json - nothing is hard-coded here.
"""
import base64
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MOCK_STATE = ROOT / "state" / "mock_d365.json"


@lru_cache
def config():
    return json.loads((ROOT / "config" / "d365.json").read_text())


def mode():
    return config()["mode"]


def enabled():
    return mode() in ("mock", "live")


def approvals_in_d365():
    return enabled() and config().get("approvals") == "d365_workflow"


# ------------------------------------------------------------------ transports

class LiveTransport:
    def __init__(self):
        missing = [v for v in ("D365_TENANT_ID", "D365_CLIENT_ID", "D365_CLIENT_SECRET") if not os.environ.get(v)]
        if missing:
            raise RuntimeError("D365 live mode needs environment variables: " + ", ".join(missing))
        self.base = config()["environment_url"].rstrip("/")
        self._token = None

    def _get_token(self):
        if not self._token:
            data = urllib.parse.urlencode({
                "client_id": os.environ["D365_CLIENT_ID"], "client_secret": os.environ["D365_CLIENT_SECRET"],
                "scope": f"{self.base}/.default", "grant_type": "client_credentials",
            }).encode()
            url = f"https://login.microsoftonline.com/{os.environ['D365_TENANT_ID']}/oauth2/v2.0/token"
            with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=30) as r:
                self._token = json.load(r)["access_token"]
        return self._token

    def request(self, method, path, body=None):
        url = path if path.startswith("https://") else self.base + path
        req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Authorization": f"Bearer {self._get_token()}", "Accept": "application/json",
                                              "Content-Type": "application/json", "OData-Version": "4.0"})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                raw = r.read()
                return r.status, (json.loads(raw) if raw and r.headers.get_content_type() == "application/json" else raw)
        except urllib.error.HTTPError as e:
            detail = e.read()[:500].decode("utf-8", "replace")
            raise RuntimeError(f"D365 {method} {path} -> {e.code}: {detail}") from e


class MockTransport:
    """Tiny OData look-alike: GET /data/<Entity>?$filter=a eq 'x' and b eq 2&$top=n, POST /data/<Entity>."""

    def _db(self):
        if not MOCK_STATE.exists():  # first use, or after a demo reset
            MOCK_STATE.parent.mkdir(parents=True, exist_ok=True)
            MOCK_STATE.write_text(json.dumps(_seed_mock(), indent=2))
        return json.loads(MOCK_STATE.read_text())

    def request(self, method, path, body=None):
        parsed = urllib.parse.urlparse(path)
        entity = parsed.path.rsplit("/", 1)[-1]
        db = self._db()
        if entity == "$metadata":
            return 200, {e: sorted({k for row in rows for k in row}) for e, rows in db.items()}
        if method == "GET":
            if entity not in db:
                raise RuntimeError(f"D365 GET {path} -> 404: entity {entity} not found")
            q = urllib.parse.parse_qs(parsed.query)
            rows = [r for r in db[entity] if _matches(r, q.get("$filter", [""])[0])]
            if "$top" in q:
                rows = rows[: int(q["$top"][0])]
            return 200, {"value": rows}
        if method == "POST":
            rec = dict(body)
            key = _write_key(entity)
            if key and not rec.get(key):
                rec[key] = f"{key[:3].upper()}-{len(db.get(entity, [])) + 1:06d}"
            db.setdefault(entity, []).append(rec)
            MOCK_STATE.write_text(json.dumps(db, indent=2))
            return 201, rec
        raise RuntimeError(f"Mock D365 does not support {method}")


def _write_key(entity):
    for spec in config()["write"].values():
        if spec["entity"] == entity:
            return spec.get("key")
    return None


def _matches(row, flt):
    if not flt:
        return True
    for cond in re.split(r"\s+and\s+", flt):
        m = re.match(r"(\w+)\s+eq\s+(?:'([^']*)'|(\S+))", cond.strip())
        if not m:
            continue
        field, sval, nval = m.groups()
        want = sval if sval is not None else nval
        if str(row.get(field)) != str(want):
            return False
    return True


@lru_cache
def transport():
    return LiveTransport() if mode() == "live" else MockTransport()


def reset_mock():
    transport.cache_clear()
    if MOCK_STATE.exists():
        MOCK_STATE.unlink()


# ------------------------------------------------------------------ OData helpers

def _company_filter(extra=None):
    parts = [f"dataAreaId eq '{config()['legal_entity']}'"]
    if extra:
        parts.append(extra)
    return " and ".join(parts)


def get_all(entity, flt=None):
    """GET every row (follows @odata.nextLink paging). cross-company=true lets dataAreaId be filtered explicitly."""
    path = f"/data/{entity}?" + urllib.parse.urlencode({"cross-company": "true", "$filter": _company_filter(flt)})
    rows = []
    while path:
        _, data = transport().request("GET", path)
        rows += data.get("value", [])
        path = data.get("@odata.nextLink")
    return rows


def create(entity, record):
    _, data = transport().request("POST", f"/data/{entity}", record)
    return data


def _rename(row, fields, reverse=False):
    """Map between the agent's names and D365 field names using config."""
    if reverse:  # agent -> D365
        return {fields[k]: v for k, v in row.items() if k in fields and v is not None}
    return {k: row.get(d) for k, d in fields.items()}


# ------------------------------------------------------------------ read: master data from D365

def load_masterdata(local):
    """Return the agent's master-data dict with vendors, POs, posted invoices and projects read from D365.

    Agent-only settings with no standard D365 field (users, vendor onboarding requester for SoD, registered email
    domains, default coding, charge-code and keyword rules) still come from the local file as an overlay."""
    cfg = config()["read"]
    data = dict(local)
    users_by_personnel = {u.get("personnel_number"): u["id"] for u in local["users"]}
    overlay = {v["account"]: v for v in local["vendors"]}
    terms = config()["payment_terms_days"]

    vendors = []
    for r in get_all(cfg["vendors"]["entity"]):
        v = _rename(r, cfg["vendors"]["fields"])
        ov = overlay.get(v["account"], {})
        vendors.append({
            **{k: ov.get(k) for k in ("requested_by", "created_by", "default_coding", "email_domains")},
            "account": v["account"], "name": v["name"], "tax_id": v["tax_id"], "currency": v["currency"],
            "status": "active" if v["on_hold"] in (None, "", "No") else f"on hold ({v['on_hold']})",
            "payment_terms_days": terms.get(v["payment_terms"], 30), "tax_group": v["tax_group"],
        })
    data["vendors"] = vendors

    lines_by_po = {}
    for r in get_all(cfg["po_lines"]["entity"]):
        l = _rename(r, cfg["po_lines"]["fields"])
        lines_by_po.setdefault(l["po"], []).append({**l, "line": int(l["line"]), "qty": float(l["qty"] or 0),
                                                    "received_qty": float(l["received_qty"] or 0),
                                                    "unit_price": float(l["unit_price"] or 0)})
    pos = []
    for r in get_all(cfg["po_headers"]["entity"]):
        h = _rename(r, cfg["po_headers"]["fields"])
        pos.append({"po": h["po"], "vendor": h["vendor"], "currency": h["currency"],
                    "status": "open" if h["status"] in ("Backorder", "Received", "Open") else str(h["status"]).lower(),
                    "buyer": users_by_personnel.get(h["buyer_personnel"]),
                    "lines": sorted(lines_by_po.get(h["po"], []), key=lambda l: l["line"])})
    data["purchase_orders"] = pos

    posted = []
    for r in get_all(cfg["posted_invoices"]["entity"]):
        p = _rename(r, cfg["posted_invoices"]["fields"])
        if not p.get("voucher"):
            continue  # unposted journals are not posted invoices
        posted.append({"vendor": p["vendor"], "invoice_number": p["invoice_number"], "date": p["date"],
                       "total": p["total"], "voucher": p["voucher"],
                       "coding": {k: p[k] for k in ("main_account", "CostCenter", "Project") if p.get(k)}})
    data["posted_invoices"] = posted

    local_projects = local.get("projects", {})
    status_map = cfg["projects"]["status_values"]
    projects = {}
    for r in get_all(cfg["projects"]["entity"]):
        p = _rename(r, cfg["projects"]["fields"])
        projects[p["id"]] = {"name": p["name"], "status": status_map.get(p["status"], str(p["status"]).lower()),
                             "manager": users_by_personnel.get(p["manager_personnel"]),
                             **{d: p.get(d) for d in ("CostCenter", "Department", "BusinessUnit")},
                             "main_account": local_projects.get(p["id"], {}).get("main_account", "620500")}
    data["projects"] = projects
    data["_source"] = f"D365 ({mode()})"
    return data


def _seed_mock():
    """Build the fake D365 database from data/masterdata.json, using the same field mapping as the live reads."""
    local = json.loads((ROOT / "data" / "masterdata.json").read_text())
    cfg, le = config()["read"], config()["legal_entity"]
    personnel = {u["id"]: u.get("personnel_number") for u in local["users"]}
    db = {}

    def put(key, row):
        spec = cfg[key]
        db.setdefault(spec["entity"], []).append({"dataAreaId": le, **_rename(row, spec["fields"], reverse=True)})

    for v in local["vendors"]:
        put("vendors", {"account": v["account"], "name": v["name"], "tax_id": v["tax_id"], "currency": v["currency"],
                        "on_hold": "No" if v["status"] == "active" else "All",
                        "payment_terms": f"Net{v['payment_terms_days']}", "tax_group": v["tax_group"]})
    for po in local["purchase_orders"]:
        put("po_headers", {"po": po["po"], "vendor": po["vendor"], "status": "Received", "currency": po["currency"],
                           "buyer_personnel": personnel.get(po["buyer"])})
        for l in po["lines"]:
            put("po_lines", {"po": po["po"], **{k: l[k] for k in ("line", "item", "description", "qty", "received_qty",
                                                                    "unit_price", "category", "CostCenter", "Department",
                                                                    "BusinessUnit")}})
    for p in local["posted_invoices"]:
        put("posted_invoices", {"vendor": p["vendor"], "invoice_number": p["invoice_number"], "date": p["date"],
                                "total": p["total"], "voucher": p["voucher"], **p["coding"]})
    status_back = {v: k for k, v in cfg["projects"]["status_values"].items()}
    for pid, p in local["projects"].items():
        put("projects", {"id": pid, "name": p["name"], "status": status_back.get(p["status"], p["status"]),
                         "manager_personnel": personnel.get(p["manager"]),
                         **{d: p[d] for d in ("CostCenter", "Department", "BusinessUnit")}})
    return db


# ------------------------------------------------------------------ write: pending invoices / journals

def _ledger_display(line):
    """Offset account display value in the order of the account structure, e.g. 618100-BU-001-CC-210-D-030."""
    segs = config()["account_structure_segments"]
    return "-".join(str(line.get(s) or "") for s in segs).rstrip("-")


def push_invoice(case, pdf_path, findings_note):
    """Create the invoice in D365 and attach the PDF and the agent's findings. Returns what was created."""
    w, le, h = config()["write"], config()["legal_entity"], case["header"]
    created = {"mode": mode(), "records": []}

    def make(spec_key, row):
        spec = w[spec_key]
        rec = create(spec["entity"], {"dataAreaId": le, **_rename(row, spec["fields"], reverse=True)})
        created["records"].append({"entity": spec["entity"], "key": rec.get(spec.get("key", "")) if spec.get("key") else None})
        return rec

    if case["invoice_type"] == "po":
        ref = case["id"]
        make("pending_invoice_header", {"reference": ref, "vendor": case["vendor_account"], "invoice_number": h["invoice_number"],
                                        "invoice_date": h["invoice_date"], "due_date": h["due_date"], "currency": h["currency"],
                                        "po": h["po_number"], "description": f"Captured by AP agent ({case['id']})"})
        lines = case["extraction"]["lines"]
        for m in case["matches"]:
            il = lines[m["invoice_line"]]
            make("pending_invoice_line", {"reference": ref, "line": m["invoice_line"] + 1, "po": h["po_number"],
                                          "po_line": m["po_line"], "item": m.get("po_item"), "qty": il["quantity"],
                                          "unit_price": il["unit_price"], "amount": il["amount"]})
        target = {"type": "Pending vendor invoice", "reference": ref, "table": "VendInvoiceInfoTable"}
    else:
        hdr = make("journal_header", {"journal_name": config()["invoice_journal_name"],
                                      "description": f"{h['vendor_name']} {h['invoice_number']} ({case['id']})"})
        batch = hdr.get(w["journal_header"]["key"])
        tg = case["accounting"].get("tax_group")
        for i, l in enumerate(case["accounting"]["lines"], 1):
            is_proj = l["account_type"] == "Project"
            make("journal_line", {"batch": batch, "line": i, "vendor": case["vendor_account"],
                                  "invoice_number": h["invoice_number"], "invoice_date": h["invoice_date"],
                                  "due_date": h["due_date"], "credit": l["amount"], "currency": h["currency"],
                                  "text": l["description"][:60], "offset_type": "Project" if is_proj else "Ledger",
                                  "offset_account": l["Project"] if is_proj else _ledger_display(l),
                                  "project_category": l.get("project_category"), "tax_group": tg,
                                  "item_tax_group": l["item_tax_group"]})
        target = {"type": "Vendor invoice journal", "reference": batch, "table": "LedgerJournalTable"}

    att = w["attachment"]
    common = {"table": target["table"], "record": target["reference"]}
    create(att["entity"], {"dataAreaId": le, **_rename({**common, "type": "File", "name": pdf_path.name,
                                                         "file_name": pdf_path.name,
                                                         "content": base64.b64encode(pdf_path.read_bytes()).decode()},
                                                        att["fields"], reverse=True)})
    create(att["entity"], {"dataAreaId": le, **_rename({**common, "type": "Note", "name": "AP agent findings",
                                                         "notes": findings_note}, att["fields"], reverse=True)})
    created["records"] += [{"entity": att["entity"], "key": "file"}, {"entity": att["entity"], "key": "note"}]
    created["target"] = target
    return created


def mock_records():
    """Records the agent has written into the mock D365, per write entity (for the demo page)."""
    if mode() != "mock" or not MOCK_STATE.exists():
        return {}
    db = json.loads(MOCK_STATE.read_text())
    out = {}
    for name, spec in config()["write"].items():
        written_fields = set(spec["fields"].values())
        rows = []
        for r in db.get(spec["entity"], []):
            # Same entity can hold seeded posted invoices (read side); show only rows this mapping writes.
            if not (set(r) - {"dataAreaId"}) <= written_fields | {spec.get("key")}:
                continue
            rows.append({k: (v[:50] + f"... ({len(v):,} chars)" if isinstance(v, str) and len(v) > 120 else v)
                         for k, v in r.items()})
        out[f"{spec['entity']} ({name.replace('_', ' ')})"] = rows
    return out


def metadata():
    """Entity set name -> set of property names, from the service's $metadata document."""
    if mode() == "mock":
        # The mock implements exactly the configured contract.
        meta = {}
        for side in ("read", "write"):
            for spec in config()[side].values():
                meta.setdefault(spec["entity"], {"dataAreaId"}).update(spec["fields"].values(), [spec.get("key")] if spec.get("key") else [])
        return meta
    import xml.etree.ElementTree as ET
    _, raw = transport().request("GET", "/data/$metadata")
    root = ET.fromstring(raw)
    types, sets = {}, {}
    for el in root.iter():
        tag = el.tag.rsplit("}", 1)[-1]
        if tag == "EntityType":
            types[el.get("Name")] = {p.get("Name") for p in el if p.tag.endswith("Property")}
        elif tag == "EntitySet":
            sets[el.get("Name")] = el.get("EntityType", "").rsplit(".", 1)[-1]
    return {s: types.get(t, set()) for s, t in sets.items()}


def check_mapping():
    """Compare every configured entity and field with the environment's $metadata."""
    meta = metadata()
    results = []
    for side in ("read", "write"):
        for name, spec in config()[side].items():
            entry = {"side": side, "name": name, "entity": spec["entity"], "verify": spec.get("verify", False)}
            if spec["entity"] not in meta:
                entry.update(status="error", error="entity set not found in $metadata (not public, or a different name)")
            else:
                missing = [f for f in spec["fields"].values() if f not in meta[spec["entity"]]]
                entry["status"] = "fields missing" if missing else "ok"
                if missing:
                    entry["missing_fields"] = missing
            results.append(entry)
    return results
