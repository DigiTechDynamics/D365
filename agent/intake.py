"""Intake: pull invoices from the configured sources, screen every attachment, and land accepted files in inbox/.

Screening happens before any AI reads the file:
  - only allowed file types, checked by content (a PDF must start with %PDF), not just by extension
  - size limit
  - the same file (by SHA-256) is never processed twice
Rejected files go to state/quarantine/ and are logged. Sender and payment-detail checks run later, per invoice,
once the vendor is known (see validation.py).
"""
import hashlib
import json
import re

from . import sources, store

INBOX = store.ROOT / "inbox"
REGISTRY = store.STATE / "intake.json"
QUARANTINE = store.STATE / "quarantine"
AGENT = "svc-ap-agent"


def registry():
    if REGISTRY.exists():
        return json.loads(REGISTRY.read_text())
    return {"seen": {}, "files": {}, "hashes": {}, "runs": []}


def _save(reg):
    store.STATE.mkdir(parents=True, exist_ok=True)
    REGISTRY.write_text(json.dumps(reg, indent=2))


def _safe_name(name):
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._") or "attachment"
    return name[:120]


def _screen(att, cfg, reg):
    name, data = att["name"], att["content"]
    ext = ("." + name.rsplit(".", 1)[-1].lower()) if "." in name else ""
    if ext not in cfg["allowed_extensions"]:
        return f"file type {ext or '(none)'} not allowed"
    if ext == ".pdf" and not data.startswith(b"%PDF"):
        return "not a real PDF (content does not match the extension)"
    if len(data) > cfg["max_size_mb"] * 1024 * 1024:
        return f"larger than {cfg['max_size_mb']} MB"
    digest = hashlib.sha256(data).hexdigest()
    if digest in reg["hashes"]:
        return f"identical file already received as {reg['hashes'][digest]}"
    return None


def run():
    cfg = sources.config()
    screening = cfg["screening"]
    reg = registry()
    summary = {"at": store.now(), "accepted": [], "rejected": [], "errors": []}
    INBOX.mkdir(exist_ok=True)

    for src in cfg["sources"]:
        if not src.get("enabled"):
            continue
        seen = set(reg["seen"].get(src["id"], []))
        try:
            envelopes = sources.fetch(src, seen)
        except Exception as e:  # one broken source must not stop the others
            summary["errors"].append(f"{src['id']}: {e}")
            store.audit("INTAKE", AGENT, "source_error", f"{src['id']}: {e}")
            continue
        for env in envelopes:
            meta = {k: env.get(k) for k in ("channel", "source_id", "location", "sender", "sender_name",
                                            "subject", "received", "uploaded_by", "ref")}
            meta["body_excerpt"] = (env.get("body") or "")[:1500]
            origin = env.get("sender") or env.get("uploaded_by") or src["id"]
            if not env["attachments"]:
                summary["rejected"].append({"from": origin, "file": None, "reason": "no attachment"})
                store.audit("INTAKE", AGENT, "ignored", f"{src['id']}: '{env.get('subject')}' from {origin} has no attachment")
            for att in env["attachments"]:
                reason = _screen(att, screening, reg)
                name = _safe_name(att["name"])
                if reason:
                    QUARANTINE.mkdir(parents=True, exist_ok=True)
                    (QUARANTINE / name).write_bytes(att["content"])
                    summary["rejected"].append({"from": origin, "file": name, "reason": reason})
                    store.audit("INTAKE", AGENT, "quarantined", f"{name} from {origin} via {src['id']}: {reason}")
                    continue
                target, n = INBOX / name, 1
                while target.exists():
                    stem, ext = name.rsplit(".", 1)
                    target, n = INBOX / f"{stem}-{n}.{ext}", n + 1
                target.write_bytes(att["content"])
                digest = hashlib.sha256(att["content"]).hexdigest()
                reg["hashes"][digest] = target.name
                reg["files"][target.name] = meta
                summary["accepted"].append({"from": origin, "file": target.name, "via": src["id"]})
                store.audit("INTAKE", AGENT, "received", f"{target.name} from {origin} via {src['id']} ({env['channel']})")
            try:
                sources.acknowledge(src, env)
            except Exception as e:
                summary["errors"].append(f"{src['id']}: could not acknowledge {env['ref']}: {e}")
            reg["seen"].setdefault(src["id"], []).append(env["ref"])

    reg["runs"] = (reg["runs"] + [summary])[-20:]
    _save(reg)
    return summary


def source_of(filename):
    """Where a file in inbox/ came from (None for files dropped in manually)."""
    return registry()["files"].get(filename)
