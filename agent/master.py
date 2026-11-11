"""Master data and policy access (stands in for D365 F&O tables in this demo)."""
import json
import re
from difflib import SequenceMatcher
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@lru_cache
def md() -> dict:
    return json.loads((ROOT / "data" / "masterdata.json").read_text())


@lru_cache
def policy() -> dict:
    return json.loads((ROOT / "config" / "policy.json").read_text())


def user(uid):
    return next((u for u in md()["users"] if u["id"] == uid), None)


def users_with_role(role):
    return [u for u in md()["users"] if u["role"] == role]


def vendor(account):
    return next((v for v in md()["vendors"] if v["account"] == account), None)


def purchase_order(po):
    return next((p for p in md()["purchase_orders"] if p["po"] == po), None) if po else None


def _digits(s):
    return re.sub(r"\D", "", s or "")


def similarity(a, b):
    return SequenceMatcher(None, (a or "").lower(), (b or "").lower()).ratio()


def find_vendor(name, tax_id):
    """Tax ID first (exact), then name similarity. Returns (vendor, method, score)."""
    if _digits(tax_id):
        for v in md()["vendors"]:
            if _digits(v["tax_id"]) == _digits(tax_id):
                return v, "tax_id", 1.0
    best = max(md()["vendors"], key=lambda v: similarity(v["name"], name))
    score = similarity(best["name"], name)
    return (best, "name", round(score, 2)) if score >= 0.85 else (None, "none", round(score, 2))
