"""Check the D365 entity/field mapping in config/d365.json against your environment.

    set D365_TENANT_ID=...  D365_CLIENT_ID=...  D365_CLIENT_SECRET=...
    (and in config/d365.json: "mode": "live", "environment_url": "https://<your-sandbox>.operations.dynamics.com")
    python scripts/check_d365.py

Read-only: it only GETs one row per entity. Run it before switching the agent to live mode.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import d365  # noqa: E402

if not d365.enabled():
    sys.exit("D365 mode is 'off' in config/d365.json")

print(f"Checking mapping against D365 ({d365.mode()}): {d365.config()['environment_url'] if d365.mode() == 'live' else 'built-in mock'}\n")
problems = 0
for r in d365.check_mapping():
    flag = {"ok": "OK  ", "fields missing": "WARN", "error": "FAIL"}[r["status"]]
    print(f"[{flag}] {r['side']:5} {r['name']:24} {r['entity']}{'  (marked verify)' if r['verify'] else ''}")
    if r.get("missing_fields"):
        print(f"         missing fields: {', '.join(r['missing_fields'])}")
    if r.get("note"):
        print(f"         {r['note']}")
    if r.get("error"):
        print(f"         {r['error']}")
    problems += r["status"] != "ok"
print("\nAll entities answered." if not problems else f"\n{problems} entity mapping(s) need attention - adjust config/d365.json.")
