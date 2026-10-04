#!/usr/bin/env python3
"""
Load-test the stack with loadtest users created by create_loadtest_users.py.

For each user: sign in → list models → one chat completion. Reports latency,
success/fail, and rough concurrency behaviour.

Usage (from repo root, after users are synced and have model access):
  python3 scripts/test_loadtest_users.py
  python3 scripts/test_loadtest_users.py --count 100 --workers 10
  python3 scripts/test_loadtest_users.py --model-substr gemini --prompt "ping"
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import statistics
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / ".env"

PREFIX = "loadtest"
EMAIL_DOMAIN = "localhost"
PASSWORD = "LoadTestPass1!"


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
    timeout: float = 120,
) -> tuple[int, object, float]:
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode() or "{}"
            elapsed = time.perf_counter() - t0
            try:
                return resp.status, json.loads(raw), elapsed
            except json.JSONDecodeError:
                return resp.status, raw, elapsed
    except urllib.error.HTTPError as e:
        elapsed = time.perf_counter() - t0
        raw = e.read().decode() if e.fp else ""
        try:
            return e.code, json.loads(raw) if raw else {}, elapsed
        except json.JSONDecodeError:
            return e.code, raw, elapsed
    except Exception as e:
        elapsed = time.perf_counter() - t0
        return 0, {"error": str(e)}, elapsed


@dataclass
class Result:
    index: int
    email: str
    ok: bool
    stage: str
    status: int
    elapsed: float
    model: str
    detail: str


def pick_model(models: list[dict], substr: str) -> str:
    ids = [m.get("id") or "" for m in models if m.get("id")]
    if not ids:
        return ""
    if substr:
        for mid in ids:
            if substr.lower() in mid.lower():
                return mid
    # Prefer cheap gemini flash if present
    for prefer in ("flash-lite", "flash", "mini", "haiku", "small"):
        for mid in ids:
            if prefer in mid.lower():
                return mid
    return ids[0]


def list_models(base: str, token: str, retries: int = 5) -> tuple[int, list[dict], float]:
    """Fetch models with retries — OWUI can return empty lists under concurrent load."""
    last_code, last_elapsed = 0, 0.0
    for attempt in range(retries):
        code, data, elapsed = http_json(
            "GET", f"{base}/api/models", token=token, timeout=90
        )
        last_code, last_elapsed = code, elapsed
        if code != 200:
            if attempt + 1 < retries:
                time.sleep(1.0 * (attempt + 1))
                continue
            return code, [], elapsed
        if isinstance(data, list):
            models = data
        elif isinstance(data, dict):
            models = list(data.get("data") or data.get("models") or [])
        else:
            models = []
        if models:
            return code, models, elapsed
        if attempt + 1 < retries:
            time.sleep(1.5 * (attempt + 1))
    return last_code, [], last_elapsed


def run_one(
    base: str,
    index: int,
    prompt: str,
    model_substr: str,
    model_retries: int = 5,
) -> Result:
    email = f"{PREFIX}{index:03d}@{EMAIL_DOMAIN}"
    code, data, elapsed = http_json(
        "POST",
        f"{base}/api/v1/auths/signin",
        body={"email": email, "password": PASSWORD},
        timeout=30,
    )
    if code != 200 or not isinstance(data, dict) or not data.get("token"):
        return Result(index, email, False, "signin", code, elapsed, "", str(data)[:200])

    token = str(data["token"])
    code, models, elapsed = list_models(base, token, retries=model_retries)
    if code != 200:
        return Result(index, email, False, "models", code, elapsed, "", "models endpoint failed")

    model = pick_model(models, model_substr)
    if not model:
        return Result(
            index,
            email,
            False,
            "models",
            code,
            elapsed,
            "",
            "no models visible (OWUI empty under load / not synced)",
        )

    chat_retries = 3
    last_detail = ""
    for attempt in range(chat_retries):
        code, data, elapsed = http_json(
            "POST",
            f"{base}/api/chat/completions",
            token=token,
            body={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
            },
            timeout=180,
        )
        if code == 200:
            return Result(index, email, True, "chat", code, elapsed, model, "ok")
        detail = data
        if isinstance(data, dict):
            detail = data.get("detail") or data.get("error") or data
        last_detail = str(detail)[:240]
        # Transient OWUI race under many OpenAI connections
        if attempt + 1 < chat_retries and (
            "not found" in last_detail.lower() or code in (429, 502, 503)
        ):
            time.sleep(1.0 * (attempt + 1))
            # Refresh model list in case cache was empty mid-flight
            _, models2, _ = list_models(base, token, retries=2)
            model2 = pick_model(models2, model_substr)
            if model2:
                model = model2
            continue
        break
    return Result(index, email, False, "chat", code, elapsed, model, last_detail)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--count", type=int, default=100, help="How many loadtest users to exercise")
    ap.add_argument("--workers", type=int, default=10, help="Concurrent workers")
    ap.add_argument("--start", type=int, default=1, help="First user index (default 1)")
    ap.add_argument("--model-substr", default="gemini", help="Prefer models containing this string")
    ap.add_argument("--prompt", default="Reply with exactly one word: ok", help="User prompt")
    ap.add_argument("--base", default="", help="OpenWebUI base URL")
    args = ap.parse_args()

    cfg = load_dotenv(ENV_FILE)
    owui_port = cfg.get("OPENWEBUI_HOST_PORT", "3001")
    base = (args.base or f"http://127.0.0.1:{owui_port}").rstrip("/")

    indexes = list(range(args.start, args.start + args.count))
    print(
        f"Testing {len(indexes)} users against {base} "
        f"(workers={args.workers}, model~{args.model_substr!r})"
    )
    t0 = time.perf_counter()
    results: list[Result] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = [
            pool.submit(run_one, base, i, args.prompt, args.model_substr)
            for i in indexes
        ]
        for fut in concurrent.futures.as_completed(futs):
            r = fut.result()
            results.append(r)
            mark = "OK" if r.ok else "FAIL"
            print(
                f"  [{mark}] {r.email} stage={r.stage} http={r.status} "
                f"{r.elapsed:.2f}s model={r.model or '-'} {r.detail[:80]}"
            )

    wall = time.perf_counter() - t0
    results.sort(key=lambda r: r.index)
    ok = [r for r in results if r.ok]
    fail = [r for r in results if not r.ok]
    chat_times = [r.elapsed for r in ok if r.stage == "chat"]

    print()
    print("======== summary ========")
    print(f"total:   {len(results)}")
    print(f"ok:      {len(ok)}")
    print(f"fail:    {len(fail)}")
    print(f"wall:    {wall:.1f}s")
    if chat_times:
        print(
            f"chat latency (ok): "
            f"p50={statistics.median(chat_times):.2f}s "
            f"avg={statistics.mean(chat_times):.2f}s "
            f"max={max(chat_times):.2f}s "
            f"min={min(chat_times):.2f}s"
        )
    if fail:
        print("failures by stage:")
        stages: dict[str, int] = {}
        for r in fail:
            stages[r.stage] = stages.get(r.stage, 0) + 1
        for stage, n in sorted(stages.items()):
            print(f"  {stage}: {n}")
        print("first failures:")
        for r in fail[:8]:
            print(f"  {r.email}: {r.stage} HTTP {r.status} — {r.detail[:160]}")

    # Non-zero exit if more than half failed
    if len(ok) == 0:
        sys.exit(2)
    if len(fail) > len(ok):
        sys.exit(1)


if __name__ == "__main__":
    import sys

    main()
