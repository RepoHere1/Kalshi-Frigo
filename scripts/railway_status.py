#!/usr/bin/env python3
"""railway_status.py -- live deploy status, domains and logs for Kalshi-Frigo.

Talks to Railway's public GraphQL API directly. The `railway` CLI is not
reliable here: it reports "Unauthorized" even with a valid account token,
because its auth path resolves separately from the API's.

Three quirks of the API are handled below:
  1. Railway's edge returns 403 for python-urllib's default User-Agent.
     Any custom UA string makes the same token return 200.
  2. `deploymentLogs` takes `deploymentId`, not `serviceId`.
  3. `Deployment.meta` is a scalar JSON blob, not a typed object, so it is
     requested bare and json.loads'd.

Note that the top-level `projects` / `me.projects` listings come back empty
for this account even though direct `project(id: ...)` lookups work, so the
IDs below are pinned.

Usage:
    python scripts/railway_status.py            # status of the latest deploy
    python scripts/railway_status.py --logs     # + build/deploy log tail
    python scripts/railway_status.py --json     # raw JSON

Token resolution (first match wins):
    $RAILWAY_TOKEN / $RAILWAY_PROJECT_TOKEN env vars, then
    D:\\master.env, ~\\master.env, <repo>\\master.env
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

API = "https://backboard.railway.com/graphql/v2"
UA = "railway-status/1.0"

PROJECT_ID = "13dc6b76-8970-448f-8039-51e549dddfff"
ENV_ID = "d4aa1d8d-e9e7-4bdc-86c2-202160144e81"
SERVICE_ID = "7a2f5d98-d6e3-4041-a8f9-0296e4bb05cc"

ENV_FILES = [
    Path("D:/master.env"),
    Path.home() / "master.env",
    Path(__file__).resolve().parent.parent / "master.env",
]


def load_token() -> str:
    """Resolve a Railway token, preferring explicit env vars."""
    for var in ("RAILWAY_TOKEN", "RAILWAY_PROJECT_TOKEN"):
        v = os.environ.get(var, "").strip()
        if v:
            return v
    for f in ENV_FILES:
        try:
            for line in f.read_text().splitlines():
                line = line.strip()
                if line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                if k.strip() in ("RAILWAY_TOKEN", "RAILWAY_PROJECT_TOKEN"):
                    v = v.strip().strip('"').strip("'")
                    if v:
                        return v
        except OSError:
            continue
    sys.exit("No Railway token found (set $RAILWAY_TOKEN or add one to master.env).")


def gql(token: str, query: str, variables: dict | None = None) -> dict:
    body = json.dumps({"query": query, "variables": variables or {}}).encode()
    req = urllib.request.Request(
        API,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            "User-Agent": UA,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            payload = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code}: {e.read().decode()[:600]}") from None
    if payload.get("errors"):
        raise RuntimeError(json.dumps(payload["errors"], indent=2)[:1200])
    return payload["data"]


def project_info(token: str) -> dict:
    return gql(
        token,
        """
        query ($id: String!) {
          project(id: $id) {
            id name
            services { edges { node { id name } } }
            environments { edges { node { id name } } }
          } }""",
        {"id": PROJECT_ID},
    )["project"]


def domains(token: str) -> list:
    """All domains attached in this environment.

    An empty list is the reason a *.up.railway.app URL 404s: Railway's edge
    has nothing to route to, even when the deployment itself succeeded.
    """
    data = gql(
        token,
        """
        query ($environmentId: String!, $projectId: String!, $serviceId: String!) {
          domains(environmentId: $environmentId, projectId: $projectId, serviceId: $serviceId) {
            serviceDomains { id domain targetPort }
            customDomains { id domain targetPort }
          } }""",
        {"environmentId": ENV_ID, "projectId": PROJECT_ID, "serviceId": SERVICE_ID},
    )["domains"]
    return data.get("serviceDomains", []) + data.get("customDomains", [])


def latest_deployments(token: str, count: int = 3) -> list:
    data = gql(
        token,
        """
        query ($projectId: String!, $environmentId: String!, $serviceId: String!, $count: Int!) {
          deployments(first: $count, input: {
            projectId: $projectId, environmentId: $environmentId,
            serviceId: $serviceId }) {
            edges { node { id status createdAt staticUrl meta } } } }""",
        {
            "projectId": PROJECT_ID,
            "environmentId": ENV_ID,
            "serviceId": SERVICE_ID,
            "count": count,
        },
    )
    out = []
    for e in data["deployments"]["edges"]:
        n = e["node"]
        meta = n.get("meta")
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except ValueError:
                meta = {}
        n["meta"] = meta or {}
        out.append(n)
    return out


def deployment_logs(token: str, deployment_id: str, limit: int = 120) -> list:
    data = gql(
        token,
        """
        query ($deploymentId: String!) {
          deploymentLogs(deploymentId: $deploymentId, limit: 500) {
            message timestamp severity } }""",
        {"deploymentId": deployment_id},
    )
    return data.get("deploymentLogs", [])[-limit:]


def main() -> int:
    token = load_token()

    try:
        p = project_info(token)
    except RuntimeError as e:
        print(f"PROJECT LOOKUP FAILED: {e}")
        return 2

    print(f"project    : {p['name']}  ({p['id']})")
    for e in p["services"]["edges"]:
        n = e["node"]
        mark = "  <- this service" if n["id"] == SERVICE_ID else ""
        print(f"  service  : {n['id']}  {n['name']}{mark}")
    for e in p["environments"]["edges"]:
        n = e["node"]
        print(f"  env      : {n['id']}  {n['name']}")

    doms = domains(token)
    if doms:
        for d in doms:
            print(f"  domain   : https://{d['domain']}  (port {d.get('targetPort')})")
    else:
        print("  domain   : NONE ATTACHED -- any *.up.railway.app URL will 404")
        print("             Fix: railway dashboard > Networking > Generate Domain")

    deps = latest_deployments(token)
    if not deps:
        print("\nNo deployments yet.")
        return 1

    d = deps[0]
    meta = d.get("meta") or {}
    print(f"\nlatest deployment")
    print(f"  id       : {d['id']}")
    print(f"  status   : {d['status']}")
    print(f"  created  : {d.get('createdAt')}")
    print(f"  staticUrl: {d.get('staticUrl') or '(none)'}")
    print(f"  commit   : {(meta.get('commitHash') or '?')[:12]} ({meta.get('branch', '?')})")
    msg = (meta.get("commitMessage") or "").splitlines()
    if msg:
        print(f"  message  : {msg[0][:110]}")

    if len(deps) > 1:
        print("\nrecent deployments:")
        for o in deps[1:]:
            sha = (o.get("meta") or {}).get("commitHash", "?")[:8]
            print(f"  {o.get('createdAt')}  {o.get('status'):<12} {sha}")

    if "--json" in sys.argv:
        print(json.dumps(d, indent=2))

    if "--logs" in sys.argv:
        print("\n--- logs (tail) ---")
        try:
            for entry in deployment_logs(token, d["id"]):
                sev = (entry.get("severity") or "").lower()
                tag = f"[{sev}] " if sev and sev != "info" else ""
                ts = str(entry.get("timestamp", ""))[:19]
                print(f"{ts} {tag}{str(entry.get('message', '')).rstrip()}")
        except RuntimeError as e:
            print(f"(log fetch failed: {e})")

    return 0


if __name__ == "__main__":
    sys.exit(main())
