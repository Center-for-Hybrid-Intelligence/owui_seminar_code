#!/usr/bin/env python3
"""Local GenAI Stack installer API + static UI (stdlib only)."""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
STATIC_DIR = Path(__file__).resolve().parent / "static"
ENV_FILE = ROOT / ".env"
STATE_FILE = ROOT / ".genaistack-state"
VERSION_FILE = ROOT / "VERSION"
RUN_SH = ROOT / "genaistack" / "_run.sh"
ERASE_SH = ROOT / "erase.sh"

PROVIDER_KEYS = [
    ("OPENAI_API_KEY", "OpenAI"),
    ("ANTHROPIC_API_KEY", "Anthropic"),
    ("GEMINI_API_KEY", "Gemini"),
    ("MISTRAL_API_KEY", "Mistral"),
    ("AZURE_OPENAI_API_KEY", "Azure OpenAI"),
]

# Fields exposed in the Configure side panel (grouped). Secrets never leave masked.
CONFIG_SCHEMA: list[dict[str, Any]] = [
    {
        "id": "providers",
        "title": "AI providers",
        "fields": [
            {"key": "OPENAI_API_KEY", "label": "OpenAI API key", "secret": True},
            {"key": "ANTHROPIC_API_KEY", "label": "Anthropic API key", "secret": True},
            {"key": "GEMINI_API_KEY", "label": "Gemini API key", "secret": True},
            {"key": "MISTRAL_API_KEY", "label": "Mistral API key", "secret": True},
            {"key": "AZURE_OPENAI_API_KEY", "label": "Azure OpenAI API key", "secret": True},
        ],
    },
    {
        "id": "owui_admin",
        "title": "Open WebUI admin",
        "fields": [
            {"key": "WEBUI_ADMIN_NAME", "label": "Name", "secret": False},
            {"key": "WEBUI_ADMIN_EMAIL", "label": "Email", "secret": False},
            {"key": "WEBUI_ADMIN_PASSWORD", "label": "Password", "secret": True},
        ],
    },
    {
        "id": "litellm_admin",
        "title": "LiteLLM admin",
        "fields": [
            {"key": "LITELLM_UI_USERNAME", "label": "Username", "secret": False},
            {"key": "LITELLM_UI_PASSWORD", "label": "Password", "secret": True},
        ],
    },
    {
        "id": "access",
        "title": "Network & ports",
        "fields": [
            {"key": "BIND_HOST", "label": "Bind host", "secret": False},
            {"key": "WEBUI_URL", "label": "Open WebUI URL", "secret": False},
            {"key": "OPENWEBUI_HOST_PORT", "label": "Open WebUI port", "secret": False},
            {"key": "LITELLM_HOST_PORT", "label": "LiteLLM port", "secret": False},
        ],
    },
    {
        "id": "complete",
        "title": "Complete extras",
        "fields": [
            {"key": "MCPO_HOST_PORT", "label": "mcpo port", "secret": False},
            {"key": "N8N_HOST_PORT", "label": "n8n port", "secret": False},
            {"key": "LANGFUSE_HOST_PORT", "label": "Langfuse port", "secret": False},
            {"key": "LANGFUSE_PUBLIC_KEY", "label": "Langfuse public key", "secret": True},
            {"key": "LANGFUSE_SECRET_KEY", "label": "Langfuse secret key", "secret": True},
        ],
    },
    {
        "id": "sync",
        "title": "User sync",
        "fields": [
            {"key": "DEFAULT_TEAM_BUDGET", "label": "Default team budget ($)", "secret": False},
            {"key": "USER_SYNC_INTERVAL", "label": "Sync interval (seconds)", "secret": False},
            {"key": "OPENWEBUI_ADMIN_API_KEY", "label": "OpenWebUI admin API key", "secret": True},
        ],
    },
]

SECRET_KEYS = {
    f["key"]
    for group in CONFIG_SCHEMA
    for f in group["fields"]
    if f.get("secret")
} | {k for k, _ in PROVIDER_KEYS}

# setup.sh generates these when missing or set to "auto". Never clear them from the UI.
AUTO_SECRET_KEYS = (
    "LITELLM_MASTER_KEY",
    "LITELLM_SALT_KEY",
    "LITELLM_UI_PASSWORD",
    "LITELLM_DB_PASSWORD",
    "POSTGRES_PASSWORD",
    "WEBUI_SECRET_KEY",
    "WEBUI_ADMIN_PASSWORD",
)

CORE_CONTAINERS = ("owui", "litellm", "postgres", "user-sync")
COMPLETE_EXTRA = ("mcpo", "n8n_2", "langfuse")

_install_lock = threading.Lock()
_install_proc: subprocess.Popen[str] | None = None
_install_log: queue.Queue[str | None] = queue.Queue()
_install_lines: list[str] = []
_install_result: dict[str, Any] = {
    "running": False,
    "exit_code": None,
    "error_code": None,
    "message": "",
    "level": None,
    "kind": None,  # "install" | "uninstall"
    "wipe_data": False,
    "started_at": None,
    "finished_at": None,
}


def package_version() -> str:
    if VERSION_FILE.is_file():
        return VERSION_FILE.read_text(encoding="utf-8").strip() or "unknown"
    return "unknown"


def read_env() -> dict[str, str]:
    cfg: dict[str, str] = {}
    if not ENV_FILE.is_file():
        return cfg
    for line in ENV_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        cfg[key.strip()] = val
    return cfg


def upsert_env(updates: dict[str, str]) -> None:
    lines: list[str] = []
    if ENV_FILE.is_file():
        lines = ENV_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
    else:
        example = ROOT / ".env.example"
        if example.is_file():
            lines = example.read_text(encoding="utf-8", errors="replace").splitlines()

    seen: set[str] = set()
    out: list[str] = []
    for line in lines:
        raw = line
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in updates:
                out.append(f"{key}={updates[key]}")
                seen.add(key)
                continue
        out.append(raw)

    for key, val in updates.items():
        if key not in seen:
            out.append(f"{key}={val}")

    ENV_FILE.write_text("\n".join(out) + "\n", encoding="utf-8")
    try:
        os.chmod(ENV_FILE, 0o600)
    except OSError:
        pass


def key_looks_valid(val: str) -> bool:
    v = (val or "").strip()
    if not v or v == "auto":
        return False
    if "your-" in v.lower() or v.endswith("-here"):
        return False
    return True


def mask_key(val: str) -> str:
    if not val or len(val) < 8:
        return "••••"
    return val[:4] + "…" + val[-4:]


def read_state_file() -> dict[str, str]:
    data: dict[str, str] = {}
    if not STATE_FILE.is_file():
        return data
    for line in STATE_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        data[k.strip()] = v.strip()
    return data


def docker_ok() -> bool:
    try:
        r = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            text=True,
            timeout=8,
        )
        return r.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False


def container_running(name: str) -> bool:
    try:
        r = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Running}}", name],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return r.returncode == 0 and r.stdout.strip().lower() == "true"
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False


def detect_level() -> dict[str, Any]:
    state = read_state_file()
    core = {n: container_running(n) for n in CORE_CONTAINERS}
    extra = {n: container_running(n) for n in COMPLETE_EXTRA}
    core_up = sum(1 for v in core.values() if v)
    extra_up = sum(1 for v in extra.values() if v)

    docker_level = 0
    if core_up >= 3:  # tolerate one flaky service
        docker_level = 1
    if core_up >= 3 and extra_up >= 2:
        docker_level = 2

    marker_level = 0
    if state.get("level", "").isdigit():
        marker_level = int(state["level"])
    elif state.get("profile") == "complete":
        marker_level = 2
    elif state.get("profile") == "minimal":
        marker_level = 1

    level = max(docker_level, marker_level)
    # If marker says installed but almost nothing is up, prefer docker truth
    if marker_level > 0 and core_up == 0 and docker_ok():
        level = 0

    profile = {0: None, 1: "minimal", 2: "complete"}.get(level)
    return {
        "level": level,
        "profile": profile or state.get("profile") or None,
        "package_version": package_version(),
        "installed_version": state.get("version") or None,
        "installed_at": state.get("installed_at") or None,
        "containers": {**core, **extra},
        "labels": {
            0: "Not installed",
            1: "Standard",
            2: "Complete",
        },
        "steps": [
            {
                "level": 0,
                "title": "Not installed",
                "includes": ["Nothing running yet"],
            },
            {
                "level": 1,
                "title": "Standard",
                "includes": [
                    "Open WebUI chat",
                    "LiteLLM gateway",
                    "PostgreSQL",
                    "User sync",
                ],
            },
            {
                "level": 2,
                "title": "Complete",
                "includes": [
                    "Everything in Standard",
                    "mcpo tools proxy",
                    "n8n automation",
                    "Langfuse observability",
                    "OpenWebUI functions",
                ],
            },
        ],
    }


def disk_free_gb(path: Path) -> float | None:
    try:
        usage = shutil.disk_usage(path)
        return round(usage.free / (1024**3), 1)
    except OSError:
        return None


def check_requirements() -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    def add(id_: str, label: str, ok: bool, detail: str, hard: bool = True) -> None:
        checks.append(
            {
                "id": id_,
                "label": label,
                "ok": ok,
                "detail": detail,
                "hard": hard,
            }
        )

    is_linux = sys.platform.startswith("linux")
    add(
        "linux",
        "Linux host",
        is_linux,
        "Linux is required" if not is_linux else f"OK ({sys.platform})",
    )

    py_ok = sys.version_info >= (3, 9)
    add(
        "python",
        "Python 3.9+",
        py_ok,
        f"Python {sys.version.split()[0]}",
    )

    docker_cli = shutil.which("docker") is not None
    add(
        "docker_cli",
        "Docker installed",
        docker_cli,
        "docker command found" if docker_cli else "Install Docker, then retry",
    )

    daemon = docker_ok() if docker_cli else False
    add(
        "docker_daemon",
        "Docker running",
        daemon,
        "Docker daemon reachable"
        if daemon
        else "Start Docker Desktop / the docker service",
    )

    compose_ok = False
    compose_detail = "docker compose not available"
    if docker_cli:
        try:
            r = subprocess.run(
                ["docker", "compose", "version"],
                capture_output=True,
                text=True,
                timeout=8,
            )
            compose_ok = r.returncode == 0
            compose_detail = (r.stdout or r.stderr or "").strip().splitlines()[:1]
            compose_detail = compose_detail[0] if compose_detail else "Compose OK"
        except (subprocess.TimeoutExpired, OSError) as e:
            compose_detail = str(e)
    add("compose", "Docker Compose v2", compose_ok, compose_detail)

    free = disk_free_gb(ROOT)
    disk_ok = free is not None and free >= 8.0
    add(
        "disk",
        "Disk space (≥ 8 GB free)",
        bool(disk_ok),
        f"{free} GB free" if free is not None else "Could not check disk",
    )

    writable = os.access(ROOT, os.W_OK)
    add(
        "writable",
        "Project folder writable",
        writable,
        str(ROOT),
    )

    genai_ok = RUN_SH.is_file()
    add(
        "package",
        "Installer package complete",
        genai_ok,
        "genaistack/_run.sh found" if genai_ok else "Missing genaistack/ — unzip the full package",
    )

    hard_ok = all(c["ok"] for c in checks if c["hard"])
    return {"ok": hard_ok, "checks": checks}


def config_status() -> dict[str, Any]:
    cfg = read_env()
    providers = []
    any_valid = False
    for key, label in PROVIDER_KEYS:
        val = cfg.get(key, "")
        valid = key_looks_valid(val)
        if valid:
            any_valid = True
        providers.append(
            {
                "key": key,
                "label": label,
                "configured": valid,
                "hint": mask_key(val) if valid else "",
            }
        )

    groups = []
    for group in CONFIG_SCHEMA:
        fields_out = []
        for f in group["fields"]:
            key = f["key"]
            val = cfg.get(key, "")
            secret = bool(f.get("secret"))
            configured = key_looks_valid(val) if secret else bool(str(val).strip())
            provider_keys = {k for k, _ in PROVIDER_KEYS}
            auto_gen = key in AUTO_SECRET_KEYS
            if secret and configured:
                placeholder = "Leave blank to keep current"
            elif secret and auto_gen:
                placeholder = "Leave blank to auto-generate"
            elif secret and key in provider_keys:
                placeholder = "Paste your API key"
            elif secret and key.startswith("LANGFUSE_"):
                placeholder = "Optional — paste if you have one"
            elif secret and key == "OPENWEBUI_ADMIN_API_KEY":
                placeholder = "Filled by installer after first run"
            elif secret:
                placeholder = "Paste secret"
            else:
                placeholder = ""
            fields_out.append(
                {
                    "key": key,
                    "label": f["label"],
                    "secret": secret,
                    "configured": configured,
                    "value": "" if secret else val,
                    "hint": "",
                    "placeholder": placeholder,
                }
            )
        groups.append({"id": group["id"], "title": group["title"], "fields": fields_out})

    return {
        "provider_configured": any_valid,
        "providers": providers,
        "groups": groups,
        "admin_email": cfg.get("WEBUI_ADMIN_EMAIL", "admin@localhost"),
        "admin_name": cfg.get("WEBUI_ADMIN_NAME", "Admin"),
        "admin_password_set": key_looks_valid(cfg.get("WEBUI_ADMIN_PASSWORD", ""))
        and cfg.get("WEBUI_ADMIN_PASSWORD") != "auto",
        "env_exists": ENV_FILE.is_file(),
    }


def ensure_env_scaffold() -> None:
    """Create .env from example if needed and keep auto-sentinels for generation."""
    if not ENV_FILE.is_file():
        example = ROOT / ".env.example"
        if example.is_file():
            shutil.copy(example, ENV_FILE)
            try:
                os.chmod(ENV_FILE, 0o600)
            except OSError:
                pass
    # Blank/missing auto secrets → "auto" so setup.sh generates them.
    # Never invent provider keys.
    cfg = read_env()
    fixes: dict[str, str] = {}
    for key in AUTO_SECRET_KEYS:
        val = (cfg.get(key) or "").strip()
        if not val:
            fixes[key] = "auto"
    if fixes:
        upsert_env(fixes)


def apply_config(body: dict[str, Any]) -> dict[str, Any]:
    updates: dict[str, str] = {}

    # Legacy single-provider payload (still accepted)
    provider = (body.get("provider") or "").strip()
    api_key = (body.get("api_key") or "").strip()
    if provider and api_key:
        allowed = {k for k, _ in PROVIDER_KEYS}
        if provider not in allowed:
            raise ValueError(f"Unknown provider key: {provider}")
        if not key_looks_valid(api_key):
            raise ValueError("That API key does not look valid.")
        updates[provider] = api_key

    for field, env_key in (
        ("admin_email", "WEBUI_ADMIN_EMAIL"),
        ("admin_name", "WEBUI_ADMIN_NAME"),
        ("admin_password", "WEBUI_ADMIN_PASSWORD"),
    ):
        # Blank password must not overwrite — leave "auto" / existing value alone.
        if field in body and body[field] is not None and str(body[field]).strip():
            updates[env_key] = str(body[field]).strip()

    # Side-panel bulk values: { "values": { "KEY": "..." } }
    values = body.get("values")
    if isinstance(values, dict):
        allowed_keys = {f["key"] for g in CONFIG_SCHEMA for f in g["fields"]}
        existing = read_env()
        for key, raw in values.items():
            if key not in allowed_keys:
                raise ValueError(f"Unknown setting: {key}")
            if raw is None:
                continue
            val = str(raw).strip()
            # Blank = unchanged. Critical for secrets still set to "auto".
            if not val:
                continue
            # Ignore masked placeholders accidentally posted back
            if key in SECRET_KEYS and ("…" in val or val.startswith("••••")):
                continue
            if key in SECRET_KEYS and val.lower() == "auto":
                # Keep sentinel; do not treat as a user password.
                continue
            if key in SECRET_KEYS and not key_looks_valid(val):
                raise ValueError(f"{key} does not look like a valid secret.")
            # Avoid rewriting identical non-secrets (noise only)
            if existing.get(key) == val:
                continue
            updates[key] = val

    ensure_env_scaffold()

    if not updates:
        # Saving with no edits is OK — still ensure auto sentinels exist.
        return config_status()

    upsert_env(updates)
    ensure_env_scaffold()
    return config_status()


def classify_error(log_text: str, exit_code: int | None) -> tuple[str, str, list[dict[str, str]]]:
    text = log_text.lower()
    actions: list[dict[str, str]] = []

    if "no valid provider api key" in text or "provider api key" in text:
        return (
            "missing_api_key",
            "An AI provider API key is required before install can finish.",
            [{"id": "configure", "label": "Add API key"}],
        )
    if "docker is required" in text or "cannot reach" in text or "docker daemon" in text:
        return (
            "docker",
            "Docker is not available. Install Docker and start it, then try again.",
            [{"id": "retry_requirements", "label": "Re-check requirements"}],
        )
    if "disk" in text and ("space" in text or "no space" in text):
        return (
            "disk",
            "Not enough free disk space. Free at least 8 GB and retry.",
            [{"id": "retry_requirements", "label": "Re-check requirements"}],
        )
    if "port" in text and ("in use" in text or "busy" in text or "address already" in text):
        return (
            "port",
            "A needed port is already in use. Close the other app or change ports in Configure / .env.",
            [{"id": "configure", "label": "Open configuration"}],
        )
    if exit_code not in (0, None):
        return (
            "install_failed",
            "Installation stopped with an error. Check the log for details.",
            [
                {"id": "show_log", "label": "Show log"},
                {"id": "configure", "label": "Check configuration"},
            ],
        )
    return ("ok", "", [])


def _reader_thread(proc: subprocess.Popen[str], kind: str = "install") -> None:
    assert proc.stdout is not None
    try:
        for line in proc.stdout:
            _install_lines.append(line.rstrip("\n"))
            _install_log.put(line.rstrip("\n"))
    finally:
        code = proc.wait()
        log_text = "\n".join(_install_lines[-200:])
        with _install_lock:
            wipe = bool(_install_result.get("wipe_data"))
        if kind == "uninstall":
            if code == 0:
                err_code, message = None, (
                    "Uninstall finished. Data volumes were deleted."
                    if wipe
                    else "Uninstall finished. Data volumes were kept."
                )
                # Drop install marker so UI shows level 0.
                try:
                    STATE_FILE.unlink(missing_ok=True)
                except OSError:
                    pass
            else:
                err_code, message = "uninstall_failed", "Uninstall stopped with an error."
        else:
            err_code, message, _actions = classify_error(log_text, code)
            if code == 0:
                message = "Install finished successfully."
        with _install_lock:
            _install_result.update(
                {
                    "running": False,
                    "exit_code": code,
                    "error_code": err_code,
                    "message": message,
                    "finished_at": time.time(),
                }
            )
        _install_log.put(None)


def start_install(level: int) -> dict[str, Any]:
    global _install_proc
    if level not in (1, 2):
        raise ValueError("level must be 1 (Standard) or 2 (Complete)")

    reqs = check_requirements()
    if not reqs["ok"]:
        raise RuntimeError("Requirements are not met. Fix the checklist first.")

    # Preserve / restore "auto" so setup.sh can generate passwords & keys.
    ensure_env_scaffold()

    cfg = config_status()
    if not cfg["provider_configured"]:
        raise RuntimeError("Add an AI provider API key in Configure before installing.")

    profile = "minimal" if level == 1 else "complete"

    with _install_lock:
        if _install_result.get("running"):
            raise RuntimeError("An install is already running.")
        while True:
            try:
                _install_log.get_nowait()
            except queue.Empty:
                break
        _install_lines.clear()
        _install_result.update(
            {
                "running": True,
                "exit_code": None,
                "error_code": None,
                "message": "Installing…",
                "level": level,
                "kind": "install",
                "wipe_data": False,
                "started_at": time.time(),
                "finished_at": None,
            }
        )

        env = os.environ.copy()
        env["GENAISTACK_PROFILE"] = profile
        # Force non-interactive docker run
        cmd = ["bash", str(RUN_SH)]
        _install_proc = subprocess.Popen(
            cmd,
            cwd=str(ROOT),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        threading.Thread(
            target=_reader_thread, args=(_install_proc, "install"), daemon=True
        ).start()

    return {"started": True, "level": level, "profile": profile}


def start_uninstall(wipe_data: bool = False) -> dict[str, Any]:
    global _install_proc
    if not ERASE_SH.is_file():
        raise RuntimeError("erase.sh is missing from this package.")

    if not docker_ok():
        raise RuntimeError("Docker must be running to uninstall.")

    with _install_lock:
        if _install_result.get("running"):
            raise RuntimeError("Another install/uninstall is already running.")
        while True:
            try:
                _install_log.get_nowait()
            except queue.Empty:
                break
        _install_lines.clear()
        _install_result.update(
            {
                "running": True,
                "exit_code": None,
                "error_code": None,
                "message": "Uninstalling…",
                "level": None,
                "kind": "uninstall",
                "wipe_data": bool(wipe_data),
                "started_at": time.time(),
                "finished_at": None,
            }
        )
        cmd = ["bash", str(ERASE_SH), "--yes"]
        if wipe_data:
            cmd.append("--wipe-data")
        _install_proc = subprocess.Popen(
            cmd,
            cwd=str(ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        threading.Thread(
            target=_reader_thread, args=(_install_proc, "uninstall"), daemon=True
        ).start()

    return {"started": True, "wipe_data": bool(wipe_data)}


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, directory=str(STATIC_DIR), **kwargs)

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _json(self, code: int, payload: Any) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        if not raw:
            return {}
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("JSON object required")
        return data

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/api/status":
            self._json(200, detect_level())
            return
        if path == "/api/requirements":
            self._json(200, check_requirements())
            return
        if path == "/api/config":
            self._json(200, config_status())
            return
        if path == "/api/install/result":
            with _install_lock:
                result = dict(_install_result)
            log_tail = "\n".join(_install_lines[-80:])
            err_code = result.get("error_code")
            actions: list[dict[str, str]] = []
            if err_code:
                _, _, actions = classify_error(log_tail, result.get("exit_code"))
            result["actions"] = actions
            result["log_tail"] = log_tail
            self._json(200, result)
            return
        if path == "/api/install/stream":
            self._sse_stream()
            return
        if path in ("/", "/index.html"):
            self.path = "/index.html"
            return super().do_GET()
        return super().do_GET()

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            if path == "/api/config":
                body = self._read_json()
                self._json(200, apply_config(body))
                return
            if path == "/api/install":
                body = self._read_json()
                level = int(body.get("level") or 0)
                self._json(200, start_install(level))
                return
            if path == "/api/uninstall":
                body = self._read_json()
                wipe = bool(body.get("wipe_data"))
                self._json(200, start_uninstall(wipe_data=wipe))
                return
        except ValueError as e:
            self._json(400, {"error": str(e)})
            return
        except RuntimeError as e:
            self._json(409, {"error": str(e)})
            return
        except Exception as e:  # noqa: BLE001
            self._json(500, {"error": str(e)})
            return
        self._json(404, {"error": "not found"})

    def _sse_stream(self) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        # Replay recent lines, then follow
        for line in list(_install_lines[-200:]):
            self._sse_send(line)

        while True:
            try:
                item = _install_log.get(timeout=1.0)
            except queue.Empty:
                try:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                except BrokenPipeError:
                    return
                with _install_lock:
                    if not _install_result.get("running") and _install_result.get("finished_at"):
                        self._sse_send(json.dumps({"event": "done", **_install_result}))
                        return
                continue
            if item is None:
                with _install_lock:
                    payload = {"event": "done", **_install_result}
                self._sse_send(json.dumps(payload))
                return
            self._sse_send(item)

    def _sse_send(self, data: str) -> None:
        try:
            self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
            self.wfile.flush()
        except BrokenPipeError:
            pass


def pick_port(preferred: int = 8765) -> int:
    import socket

    for port in range(preferred, preferred + 40):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError("No free port for installer UI")


def main() -> None:
    if not STATIC_DIR.is_dir():
        print(f"Missing UI files at {STATIC_DIR}", file=sys.stderr)
        sys.exit(1)

    port = int(os.environ.get("GENAISTACK_UI_PORT") or pick_port())
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}/"
    print(f"GenAI Stack installer: {url}")
    print("Leave this terminal open. Press Ctrl+C to stop.")
    if os.environ.get("GENAISTACK_UI_NO_BROWSER") != "1":
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nInstaller stopped.")
    finally:
        server.server_close()
        with _install_lock:
            if _install_proc and _install_proc.poll() is None:
                _install_proc.terminate()


if __name__ == "__main__":
    main()
