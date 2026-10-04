#!/usr/bin/env python3
"""
Create N OpenWebUI users, each with their own group (1:1), then enforce a
LiteLLM team budget of $BUDGET per group after user-sync has provisioned teams.

Budgets live on LiteLLM *teams* (one per OpenWebUI group), not on users.
user-sync creates those teams from groups — run this script, then wait for /
restart user-sync, then this script sets max_budget=$BUDGET on each loadtest team.

Usage (from repo root, stack running):
  python3 scripts/create_loadtest_users.py
  python3 scripts/create_loadtest_users.py --count 100 --budget 2
  python3 scripts/create_loadtest_users.py --sync-wait 120
  python3 scripts/create_loadtest_users.py --cleanup   # remove loadtest users/groups
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / ".env"

PREFIX = "loadtest"
EMAIL_DOMAIN = "localhost"
PASSWORD = "LoadTestPass1!"  # shared password for all loadtest users


def load_dotenv(path: Path) -> dict[str, str]:
    cfg: dict[str, str] = {}
    if not path.is_file():
        return cfg
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        cfg[k.strip()] = v
    return cfg


def http_json(
    method: str,
    url: str,
    token: str | None = None,
    body: dict | None = None,
    timeout: float = 60,
) -> tuple[int, object]:
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode() or "{}"
            try:
                return resp.status, json.loads(raw)
            except json.JSONDecodeError:
                return resp.status, raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode() if e.fp else ""
        try:
            return e.code, json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            return e.code, raw


def email_for(i: int) -> str:
    return f"{PREFIX}{i:03d}@{EMAIL_DOMAIN}"


def group_name_for(i: int) -> str:
    return f"{PREFIX}{i:03d}"


def admin_token(base: str, email: str, password: str) -> str:
    code, data = http_json(
        "POST",
        f"{base}/api/v1/auths/signin",
        body={"email": email, "password": password},
    )
    if code != 200 or not isinstance(data, dict) or not data.get("token"):
        raise SystemExit(f"Admin sign-in failed (HTTP {code}): {data}")
    return str(data["token"])


def list_users(base: str, token: str) -> list[dict]:
    """OpenWebUI paginates /api/v1/users/ (often 30/page) — walk all pages."""
    out: list[dict] = []
    page = 1
    while True:
        code, data = http_json(
            "GET", f"{base}/api/v1/users/?page={page}&limit=100", token=token
        )
        if code != 200:
            raise SystemExit(f"List users failed (HTTP {code}): {data}")
        if isinstance(data, list):
            return data
        if not isinstance(data, dict):
            raise SystemExit(f"Unexpected users payload: {type(data)}")
        batch = list(data.get("users") or [])
        out.extend(batch)
        total = int(data.get("total") or len(out))
        if not batch or len(out) >= total:
            break
        page += 1
        if page > 200:
            break
    return out


def list_groups(base: str, token: str) -> list[dict]:
    code, data = http_json("GET", f"{base}/api/v1/groups/", token=token)
    if code != 200:
        raise SystemExit(f"List groups failed (HTTP {code}): {data}")
    if isinstance(data, dict):
        return list(data.get("items") or data.get("groups") or data.get("data") or [])
    return list(data or [])


def ensure_user(base: str, token: str, i: int, by_email: dict[str, dict]) -> str:
    email = email_for(i)
    if email in by_email:
        return str(by_email[email]["id"])
    code, data = http_json(
        "POST",
        f"{base}/api/v1/auths/add",
        token=token,
        body={
            "name": f"Load Test {i:03d}",
            "email": email,
            "password": PASSWORD,
            "role": "user",
        },
    )
    if code == 200 and isinstance(data, dict) and data.get("id"):
        by_email[email] = data
        return str(data["id"])
    # Already registered but missing from a stale cache — refresh once.
    detail = str(data)
    if code in (400, 409) and "already" in detail.lower():
        for u in list_users(base, token):
            by_email[u.get("email") or ""] = u
        if email in by_email:
            return str(by_email[email]["id"])
    raise SystemExit(f"Create user {email} failed (HTTP {code}): {data}")


def ensure_group(base: str, token: str, i: int, by_name: dict[str, dict]) -> str:
    name = group_name_for(i)
    if name in by_name:
        return str(by_name[name]["id"])
    code, data = http_json(
        "POST",
        f"{base}/api/v1/groups/create",
        token=token,
        body={
            "name": name,
            "description": f"Load-test group for {email_for(i)}",
        },
    )
    if code != 200 or not isinstance(data, dict) or not data.get("id"):
        raise SystemExit(f"Create group {name} failed (HTTP {code}): {data}")
    by_name[name] = data
    return str(data["id"])


def ensure_membership(base: str, token: str, group_id: str, user_id: str) -> None:
    code, data = http_json(
        "POST",
        f"{base}/api/v1/groups/id/{group_id}/users/add",
        token=token,
        body={"user_ids": [user_id]},
    )
    if code == 200:
        return
    # Older OpenWebUI fallback
    code2, data2 = http_json(
        "POST",
        f"{base}/api/v1/groups/id/{group_id}/update",
        token=token,
        body={"user_ids": [user_id]},
    )
    if code2 != 200:
        raise SystemExit(
            f"Add user {user_id} to group {group_id} failed "
            f"(add HTTP {code}: {data}; update HTTP {code2}: {data2})"
        )


def list_litellm_teams(litellm: str, master_key: str) -> list[dict]:
    """LiteLLM /v2/team/list is paginated (default page_size=10)."""
    out: list[dict] = []
    page = 1
    while True:
        code, data = http_json(
            "GET",
            f"{litellm}/v2/team/list?page={page}&page_size=100",
            token=master_key,
            timeout=60,
        )
        if code != 200:
            # Fallback: unpaginated legacy endpoint
            code2, data2 = http_json(
                "GET", f"{litellm}/team/list", token=master_key, timeout=60
            )
            if code2 == 200 and isinstance(data2, list):
                return data2
            raise SystemExit(f"LiteLLM team list failed (HTTP {code}): {data}")
        if isinstance(data, list):
            return data
        if not isinstance(data, dict):
            raise SystemExit(f"Unexpected team list payload: {type(data)}")
        batch = list(data.get("teams") or data.get("data") or [])
        out.extend(batch)
        total_pages = int(data.get("total_pages") or 1)
        if page >= total_pages or not batch:
            break
        page += 1
        if page > 200:
            break
    return out


def set_team_budget(litellm: str, master_key: str, team_id: str, budget: float) -> None:
    code, data = http_json(
        "POST",
        f"{litellm}/team/update",
        token=master_key,
        body={"team_id": team_id, "max_budget": budget},
    )
    if code != 200:
        raise SystemExit(f"Set budget for team {team_id} failed (HTTP {code}): {data}")


def wait_for_teams(
    litellm: str, master_key: str, expected_names: set[str], timeout: float
) -> list[dict]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        teams = list_litellm_teams(litellm, master_key)
        aliases = {t.get("team_alias") for t in teams}
        missing = expected_names - aliases
        if not missing:
            return teams
        print(f"  waiting for user-sync… {len(missing)} teams missing", flush=True)
        time.sleep(5)
    raise SystemExit(
        f"Timed out waiting for LiteLLM teams. Restart user-sync and re-run with "
        f"--budgets-only, or increase --sync-wait."
    )


def cleanup(base: str, token: str, litellm: str, master_key: str, count: int) -> None:
    print(f"Cleaning up {PREFIX}* users/groups (1..{count})…")
    users = {u.get("email"): u for u in list_users(base, token)}
    groups = {g.get("name"): g for g in list_groups(base, token)}
    for i in range(1, count + 1):
        email = email_for(i)
        name = group_name_for(i)
        g = groups.get(name)
        if g:
            gid = g["id"]
            for method, path in (
                ("DELETE", f"/api/v1/groups/id/{gid}/delete"),
                ("DELETE", f"/api/v1/groups/id/{gid}"),
                ("POST", f"/api/v1/groups/id/{gid}/delete"),
            ):
                code, _ = http_json(method, f"{base}{path}", token=token)
                if code == 200:
                    print(f"  deleted group {name}")
                    break
        u = users.get(email)
        if u:
            uid = u["id"]
            for method, path in (
                ("DELETE", f"/api/v1/users/{uid}"),
                ("DELETE", f"/api/v1/users/{uid}/delete"),
                ("POST", f"/api/v1/users/{uid}/delete"),
            ):
                code, _ = http_json(method, f"{base}{path}", token=token)
                if code == 200:
                    print(f"  deleted user {email}")
                    break
    # Best-effort: leave LiteLLM teams (user-sync does not delete them).
    print("Note: LiteLLM teams for deleted groups may remain — remove in LiteLLM UI if needed.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--count", type=int, default=100, help="Number of users/groups (default 100)")
    ap.add_argument("--budget", type=float, default=2.0, help="USD max_budget per LiteLLM team (default 2)")
    ap.add_argument("--sync-wait", type=float, default=180, help="Seconds to wait for user-sync teams")
    ap.add_argument("--budgets-only", action="store_true", help="Only set budgets on existing loadtest teams")
    ap.add_argument("--skip-budgets", action="store_true", help="Create users/groups only; do not wait/set budgets")
    ap.add_argument(
        "--restart-sync",
        action="store_true",
        help="Recreate user-sync after creating users so teams provision immediately",
    )
    ap.add_argument("--cleanup", action="store_true", help="Delete loadtest users/groups instead of creating")
    ap.add_argument("--base", default="", help="OpenWebUI base URL (default from .env)")
    ap.add_argument("--litellm", default="", help="LiteLLM base URL (default from .env)")
    args = ap.parse_args()

    cfg = load_dotenv(ENV_FILE)
    owui_port = cfg.get("OPENWEBUI_HOST_PORT", "3001")
    litellm_port = cfg.get("LITELLM_HOST_PORT", "4000")
    base = (args.base or f"http://127.0.0.1:{owui_port}").rstrip("/")
    litellm = (args.litellm or f"http://127.0.0.1:{litellm_port}").rstrip("/")
    admin_email = cfg.get("WEBUI_ADMIN_EMAIL", "admin@localhost")
    admin_password = cfg.get("WEBUI_ADMIN_PASSWORD", "")
    master_key = cfg.get("LITELLM_MASTER_KEY", "")
    if not admin_password:
        raise SystemExit("WEBUI_ADMIN_PASSWORD missing from .env")
    if not master_key and not args.skip_budgets:
        raise SystemExit("LITELLM_MASTER_KEY missing from .env")

    token = admin_token(base, admin_email, admin_password)

    if args.cleanup:
        cleanup(base, token, litellm, master_key, args.count)
        return

    expected = {group_name_for(i) for i in range(1, args.count + 1)}

    if not args.budgets_only:
        print(f"Creating {args.count} users + groups (password={PASSWORD!r})…")
        by_email = {u.get("email") or "": u for u in list_users(base, token)}
        by_name = {g.get("name") or "": g for g in list_groups(base, token)}
        print(f"  existing users={len(by_email)} groups={len(by_name)}")
        for i in range(1, args.count + 1):
            user_id = ensure_user(base, token, i, by_email)
            group_id = ensure_group(base, token, i, by_name)
            ensure_membership(base, token, group_id, user_id)
            if i == 1 or i % 10 == 0 or i == args.count:
                print(f"  {i}/{args.count} {email_for(i)} → group {group_name_for(i)}")
        print("OpenWebUI users/groups ready.")
        print()
        if args.restart_sync:
            print("Recreating user-sync…")
            subprocess.run(
                [
                    "docker",
                    "compose",
                    "-f",
                    "docker-compose.yml",
                    "-f",
                    "genaistack/docker-compose.genaistack.yml",
                    "up",
                    "-d",
                    "--force-recreate",
                    "--no-deps",
                    "user-sync",
                ],
                cwd=str(ROOT),
                check=False,
            )
        else:
            print("Next: let user-sync provision LiteLLM teams (or pass --restart-sync):")
            print("  docker compose up -d --force-recreate --no-deps user-sync")
        print()

    if args.skip_budgets:
        print("Skipping budget wait/update (--skip-budgets).")
        return

    print(f"Waiting up to {args.sync_wait:.0f}s for LiteLLM teams…")
    teams = wait_for_teams(litellm, master_key, expected, args.sync_wait)
    # Update every team whose alias matches (including duplicate leftovers from
    # partial syncs). /v2/team/list is paginated — list_litellm_teams walks pages.
    updated = 0
    for t in teams:
        alias = t.get("team_alias") or ""
        if alias not in expected and not (
            alias.startswith(PREFIX) and alias[len(PREFIX) :].isdigit()
        ):
            continue
        tid = t.get("team_id")
        if not tid:
            continue
        if t.get("max_budget") == args.budget:
            continue
        set_team_budget(litellm, master_key, str(tid), args.budget)
        updated += 1
    # Re-list to confirm
    teams = list_litellm_teams(litellm, master_key)
    load = [
        t
        for t in teams
        if (t.get("team_alias") or "").startswith(PREFIX)
        and (t.get("team_alias") or "")[len(PREFIX) :].isdigit()
    ]
    at_budget = sum(1 for t in load if t.get("max_budget") == args.budget)
    print(
        f"Updated {updated} teams; "
        f"{at_budget}/{len(load)} loadtest* teams now at max_budget=${args.budget:g}."
    )
    if at_budget < len(expected):
        print(
            "WARNING: fewer teams at target budget than expected aliases. "
            "Check LiteLLM for duplicate/orphan teams."
        )
    print()
    print("Credentials:")
    print(f"  email:    {PREFIX}001@{EMAIL_DOMAIN} … {PREFIX}{args.count:03d}@{EMAIL_DOMAIN}")
    print(f"  password: {PASSWORD}")
    print(f"  group:    same as local-part ({PREFIX}001, …)")
    print()
    print("Test with: python3 scripts/test_loadtest_users.py")


if __name__ == "__main__":
    main()
