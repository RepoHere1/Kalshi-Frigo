"""Pull closed trades and dump the fields that explain sizing vs outcome."""
import json
import subprocess
import sys
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
RAILWAY = r"C:\Users\Device\AppData\Roaming\npm\railway.exe"
BASE = "https://kalshi-frigo-production.up.railway.app"

token = json.loads(subprocess.run(
    [RAILWAY, "variables", "--service", "kalshi-frigo", "--json"],
    capture_output=True, text=True, timeout=60,
).stdout)["DASHBOARD_TOKEN"]

req = urllib.request.Request(BASE + "/api/trades", headers={"X-Auth-Token": token})
with urllib.request.urlopen(req, timeout=60) as r:
    data = json.loads(r.read().decode())

rows = data.get("trades", data) if isinstance(data, dict) else data
print("closed trades:", len(rows) if isinstance(rows, list) else "?")
if isinstance(rows, list):
    for t in rows:
        print(json.dumps(t, default=str)[:260])
