"""Invoice sources: a shared AP mailbox and a SharePoint document library.

Each source yields "envelopes" (one email, or one SharePoint file) with their attachments, and is acknowledged
after intake so nothing is read twice:
  - demo mode: local folders stand in for Microsoft 365; acknowledgements are recorded in state/intake.json
  - graph mode: Microsoft Graph (app-only). Email is marked read and moved; SharePoint files are moved to a
    processed folder.

Graph permissions to request from IT (least privilege):
  - Mail.ReadWrite (application), restricted to the AP mailbox with an Exchange application access policy /
    RBAC for Applications - not every mailbox in the tenant.
  - Sites.Selected (application), then grant the app 'write' on the Finance site only.
"""
import base64
import email
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from email import policy as email_policy
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GRAPH = "https://graph.microsoft.com/v1.0"


def config():
    return json.loads((ROOT / "config" / "sources.json").read_text())


# ------------------------------------------------------------------ demo mode (local folders)

def _demo_mailbox(src, seen):
    for path in sorted((ROOT / src["demo_folder"]).glob("*.eml")):
        msg = email.message_from_bytes(path.read_bytes(), policy=email_policy.default)
        ref = msg["Message-ID"] or path.name
        if ref in seen:
            continue
        body = msg.get_body(preferencelist=("plain",))
        received = msg["Date"]
        yield {
            "ref": ref, "channel": "email", "source_id": src["id"], "location": src["mailbox"],
            "sender": parseaddr(msg["From"])[1].lower(), "sender_name": parseaddr(msg["From"])[0],
            "subject": msg["Subject"] or "", "body": body.get_content().strip() if body else "",
            "received": parsedate_to_datetime(received).isoformat() if received else None,
            "attachments": [{"name": p.get_filename() or "attachment", "content": p.get_payload(decode=True) or b""}
                            for p in msg.iter_attachments()],
        }


def _demo_sharepoint(src, seen):
    folder = ROOT / src["demo_folder"]
    for path in sorted(p for p in folder.glob("*") if p.is_file()):
        ref = f"{src['folder']}/{path.name}"
        if ref in seen:
            continue
        yield {
            "ref": ref, "channel": "sharepoint", "source_id": src["id"], "location": f"{src['site']} / {src['folder']}",
            "uploaded_by": src.get("demo_uploaded_by"), "received": None, "subject": "", "body": "",
            "attachments": [{"name": path.name, "content": path.read_bytes()}],
        }


# ------------------------------------------------------------------ Microsoft Graph mode

class Graph:
    """Minimal app-only Graph client (client credentials). Secrets come from environment variables only."""

    def __init__(self):
        missing = [v for v in ("GRAPH_TENANT_ID", "GRAPH_CLIENT_ID", "GRAPH_CLIENT_SECRET") if not os.environ.get(v)]
        if missing:
            raise RuntimeError("Graph mode needs environment variables: " + ", ".join(missing))
        self._token = None

    def token(self):
        if not self._token:
            data = urllib.parse.urlencode({
                "client_id": os.environ["GRAPH_CLIENT_ID"], "client_secret": os.environ["GRAPH_CLIENT_SECRET"],
                "scope": "https://graph.microsoft.com/.default", "grant_type": "client_credentials",
            }).encode()
            url = f"https://login.microsoftonline.com/{os.environ['GRAPH_TENANT_ID']}/oauth2/v2.0/token"
            with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=30) as r:
                self._token = json.load(r)["access_token"]
        return self._token

    def call(self, method, path, body=None, raw=False):
        url = path if path.startswith("https://") else GRAPH + path
        req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Authorization": f"Bearer {self.token()}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                payload = r.read()
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"Graph {method} {path} failed: {e.code} {e.read()[:300]!r}") from e
        return payload if raw else (json.loads(payload) if payload else {})


def _graph_mailbox(src, seen, g):
    box = urllib.parse.quote(src["mailbox"])
    cfg = src["graph"]
    q = urllib.parse.urlencode({
        "$filter": "isRead eq false and hasAttachments eq true",
        "$select": "id,subject,from,receivedDateTime,body", "$top": str(cfg.get("max_messages", 25)),
    })
    for m in g.call("GET", f"/users/{box}/mailFolders/{cfg['folder']}/messages?{q}").get("value", []):
        if m["id"] in seen:
            continue
        atts = g.call("GET", f"/users/{box}/messages/{m['id']}/attachments").get("value", [])
        yield {
            "ref": m["id"], "channel": "email", "source_id": src["id"], "location": src["mailbox"],
            "sender": m["from"]["emailAddress"]["address"].lower(), "sender_name": m["from"]["emailAddress"].get("name"),
            "subject": m.get("subject") or "", "body": _strip_html(m.get("body", {}).get("content", "")),
            "received": m.get("receivedDateTime"),
            "attachments": [{"name": a["name"], "content": base64.b64decode(a["contentBytes"])}
                            for a in atts if a.get("@odata.type") == "#microsoft.graph.fileAttachment" and a.get("contentBytes")],
        }


def _graph_sharepoint(src, seen, g):
    site = g.call("GET", f"/sites/{src['site']}")
    folder = urllib.parse.quote(src["folder"])
    items = g.call("GET", f"/sites/{site['id']}/drive/root:/{folder}:/children?$top={src['graph'].get('max_items', 50)}")
    for it in items.get("value", []):
        if "file" not in it or it["id"] in seen:
            continue
        content = g.call("GET", f"/sites/{site['id']}/drive/items/{it['id']}/content", raw=True)
        yield {
            "ref": it["id"], "channel": "sharepoint", "source_id": src["id"], "location": f"{src['site']} / {src['folder']}",
            "uploaded_by": (it.get("createdBy", {}).get("user") or {}).get("email"), "received": it.get("createdDateTime"),
            "subject": "", "body": "", "site_id": site["id"],
            "attachments": [{"name": it["name"], "content": content}],
        }


def _strip_html(s):
    import re
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s or "")).strip()


# ------------------------------------------------------------------ public API

def fetch(src, seen):
    if src["mode"] == "demo":
        return list((_demo_mailbox if src["type"] == "mailbox" else _demo_sharepoint)(src, seen))
    g = Graph()
    return list((_graph_mailbox if src["type"] == "mailbox" else _graph_sharepoint)(src, seen, g))


def acknowledge(src, env):
    """Make sure an envelope is not picked up again. Demo mode: the caller records env['ref'] as seen."""
    if src["mode"] == "demo":
        return
    g = Graph()
    if src["type"] == "mailbox":
        box = urllib.parse.quote(src["mailbox"])
        g.call("PATCH", f"/users/{box}/messages/{env['ref']}", {"isRead": True})
        target = src["graph"].get("processed_folder")
        if target:
            folders = g.call("GET", f"/users/{box}/mailFolders?$filter=displayName eq '{target}'").get("value", [])
            if folders:
                g.call("POST", f"/users/{box}/messages/{env['ref']}/move", {"destinationId": folders[0]["id"]})
    else:
        dest = g.call("GET", f"/sites/{env['site_id']}/drive/root:/{urllib.parse.quote(src['graph']['processed_folder'])}")
        g.call("PATCH", f"/sites/{env['site_id']}/drive/items/{env['ref']}", {"parentReference": {"id": dest["id"]}})
