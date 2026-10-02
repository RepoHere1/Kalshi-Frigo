import json, sys
data = json.load(sys.stdin)
logs = data.get('logs', [])
for l in logs[-10:]:
    print(l[:200])