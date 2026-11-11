"""Case storage and an append-only audit log (JSON files, so they are easy to inspect)."""
import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "state"
CASES = STATE / "cases"
AUDIT = STATE / "audit.jsonl"
OUTBOX = ROOT / "outbox"


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def audit(case_id, actor, action, detail="", data=None):
    STATE.mkdir(parents=True, exist_ok=True)
    entry = {"ts": now(), "case": case_id, "actor": actor,
             "actor_type": "agent" if actor == "svc-ap-agent" else "user", "action": action, "detail": detail}
    if data:
        entry["data"] = data
    with AUDIT.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def read_audit(case_id=None):
    if not AUDIT.exists():
        return []
    rows = [json.loads(l) for l in AUDIT.read_text(encoding="utf-8").splitlines() if l.strip()]
    return [r for r in rows if case_id is None or r["case"] == case_id]


def save(case):
    CASES.mkdir(parents=True, exist_ok=True)
    (CASES / f"{case['id']}.json").write_text(json.dumps(case, indent=2))


def load(case_id):
    p = CASES / f"{case_id}.json"
    return json.loads(p.read_text()) if p.exists() else None


def all_cases():
    if not CASES.exists():
        return []
    return sorted((json.loads(p.read_text()) for p in CASES.glob("*.json")), key=lambda c: c["id"])


def next_id():
    nums = [int(c["id"].split("-")[1]) for c in all_cases()]
    return f"CI-{(max(nums) + 1 if nums else 482):06d}"
