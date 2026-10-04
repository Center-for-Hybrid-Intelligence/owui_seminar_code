#!/usr/bin/env bash
# In-container genaistack setup: detect/repair, start profile, provision admin + default group.
# Invoked by entrypoint.sh. Expects GENAISTACK_HOME and GENAISTACK_PROFILE.
set -euo pipefail

die() { echo "ERROR: $*" >&2; exit 1; }
info() { echo "==> $*"; }
fixed() { echo "fixed: $*"; }

ROOT="${GENAISTACK_HOME}"
PROFILE="${GENAISTACK_PROFILE}"
ENV_FILE="${ROOT}/.env"
COMPOSE_DIR="$ROOT"
RUNTIME_CFG="${ROOT}/genaistack/litellm-config.runtime.yaml"
COMPOSE_OVERRIDE="${ROOT}/genaistack/docker-compose.genaistack.yml"
STACK_CONTAINERS=(owui litellm postgres user-sync)
[[ "$PROFILE" == "complete" ]] && STACK_CONTAINERS+=(mcpo n8n_2 langfuse)
OPTIONAL_STOP=(n8n mcpo langfuse ollama)
[[ "$PROFILE" == "complete" ]] && OPTIONAL_STOP=(ollama)

cd "$ROOT"

# ─── env helpers ─────────────────────────────────────────────────────────────

declare -A CFG=()

load_env() {
  CFG=()
  [[ -f "$ENV_FILE" ]] || return 0
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line#"${line%%[![:space:]]*}"}"
    [[ -z "$line" || "$line" == \#* ]] && continue
    [[ "$line" != *=* ]] && continue
    key="${line%%=*}"
    val="${line#*=}"
    key="${key%"${key##*[![:space:]]}"}"
    CFG["$key"]="$val"
  done < "$ENV_FILE"
}

upsert_env() {
  local key="$1" val="$2" tmp
  tmp="$(mktemp)"
  chmod 600 "$tmp"
  printf '%s' "$val" > "$tmp"
  python3 - "$ENV_FILE" "$key" "$tmp" <<'PY'
import sys
from pathlib import Path
path, key, src = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3])
val = src.read_text()
lines = path.read_text().splitlines() if path.exists() else []
out, found = [], False
for line in lines:
    if line.strip() and not line.strip().startswith("#") and "=" in line and line.split("=", 1)[0].strip() == key:
        out.append(f"{key}={val}")
        found = True
    else:
        out.append(line)
if not found:
    out.append(f"{key}={val}")
path.write_text("\n".join(out) + "\n")
path.chmod(0o600)
PY
  rm -f "$tmp"
  CFG["$key"]="$val"
}

rand_hex() { openssl rand -hex 24; }
rand_sk() { echo "sk-$(openssl rand -hex 24)"; }
rand_pw() { openssl rand -base64 18 | tr -d '/+=' | head -c 20; }

compose() {
  (cd "$COMPOSE_DIR" && docker compose -f docker-compose.yml -f genaistack/docker-compose.genaistack.yml "$@")
}

# Run a long docker/compose command quietly; one-line heartbeat; log only on failure.
run_quiet() {
  local label="$1"
  shift
  local log pid status spin i elapsed
  log="$(mktemp)"
  info "${label}"
  "$@" >"$log" 2>&1 &
  pid=$!
  spin='|/-\\'
  i=0
  elapsed=0
  if [[ -t 1 ]]; then
    while kill -0 "$pid" 2>/dev/null; do
      printf '\r    %s working… %ss   ' "${spin:$((i % 4)):1}" "$elapsed"
      i=$((i + 1))
      sleep 1
      elapsed=$((elapsed + 1))
    done
    wait "$pid"
    status=$?
    if [[ "$status" -eq 0 ]]; then
      printf '\r    done (%ss).                    \n' "$elapsed"
    else
      printf '\r    failed after %ss.              \n' "$elapsed"
    fi
  else
    wait "$pid"
    status=$?
  fi
  if [[ "$status" -eq 0 ]]; then
    rm -f "$log"
    return 0
  fi
  echo "ERROR: ${label} failed. Last log lines:" >&2
  tail -n 80 "$log" >&2 || true
  echo "Full log: ${log}" >&2
  return 1
}

# ─── provider key ────────────────────────────────────────────────────────────

PROVIDER_KEYS=(OPENAI_API_KEY ANTHROPIC_API_KEY AZURE_OPENAI_API_KEY MISTRAL_API_KEY GEMINI_API_KEY)

is_real_key() {
  local v="${1:-}"
  [[ -n "$v" && "$v" != auto && "$v" != *your-* ]]
}

prompt_provider_key() {
  local choice key_name key_val
  if [[ ! -t 0 ]]; then
    die "No API key found. Run ./install.sh from a terminal and paste your key when asked."
  fi
  echo
  echo "Which AI provider is your API key from?"
  echo "  1) OpenAI"
  echo "  2) Anthropic (Claude)"
  echo "  3) Azure OpenAI"
  echo "  4) Mistral"
  echo "  5) Google Gemini"
  read -r -p "Choice [1-5]: " choice
  case "$choice" in
    1) key_name=OPENAI_API_KEY ;;
    2) key_name=ANTHROPIC_API_KEY ;;
    3) key_name=AZURE_OPENAI_API_KEY ;;
    4) key_name=MISTRAL_API_KEY ;;
    5) key_name=GEMINI_API_KEY ;;
    *) die "Please answer with a number from 1 to 5." ;;
  esac
  echo
  echo "Paste your API key, then press Enter."
  echo "(Characters stay hidden for privacy.)"
  read -r -s -p "API key: " key_val
  echo
  is_real_key "$key_val" || die "That does not look like a real API key. Check and run ./install.sh again."
  export "$key_name=$key_val"
}

ensure_provider_key() {
  local k found=0
  for k in "${PROVIDER_KEYS[@]}"; do
    if is_real_key "${!k:-}"; then
      found=1
      break
    fi
  done
  if [[ "$found" -eq 0 ]]; then
    load_env
    for k in "${PROVIDER_KEYS[@]}"; do
      if is_real_key "${CFG[$k]:-}"; then
        found=1
        break
      fi
    done
  fi
  if [[ "$found" -eq 0 ]]; then
    prompt_provider_key
  fi
}

# ─── ports ───────────────────────────────────────────────────────────────────

# Host ports claimed by ensure_port_var in this run (not listening yet).
declare -A RESERVED_HOST_PORTS=()

port_in_use() {
  local port="$1"
  # ss is not always in the installer image; use python
  python3 - "$port" <<'PY'
import socket, sys
port = int(sys.argv[1])
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
try:
    s.bind(("127.0.0.1", port))
except OSError:
    sys.exit(0)  # in use
else:
    s.close()
    sys.exit(1)  # free
PY
}

stack_owns_port() {
  local port="$1" c
  for c in "${STACK_CONTAINERS[@]}"; do
    # HostPort is the published host port; container port keys look like "8080/tcp".
    if docker inspect "$c" --format '{{json .NetworkSettings.Ports}}' 2>/dev/null \
      | grep -qE "\"HostPort\":\"${port}\""; then
      return 0
    fi
  done
  return 1
}

port_is_available() {
  local port="$1"
  [[ -n "${RESERVED_HOST_PORTS[$port]:-}" ]] && return 1
  if port_in_use "$port"; then
    stack_owns_port "$port"
    return $?
  fi
  return 0
}

find_free_port() {
  local start="$1" p
  for p in $(seq "$start" $((start + 200))); do
    if port_is_available "$p"; then
      echo "$p"
      return 0
    fi
  done
  die "Could not find a free port near ${start}."
}

ensure_port_var() {
  local var="$1" default="$2" current chosen
  current="${CFG[$var]:-$default}"
  if [[ ! "$current" =~ ^[0-9]+$ ]] || (( current < 1 || current > 65535 )); then
    current="$default"
  fi
  if port_is_available "$current"; then
    chosen="$current"
  else
    chosen="$(find_free_port "$((current + 1))")"
    fixed "${var}: ${current} busy → ${chosen}"
  fi
  upsert_env "$var" "$chosen"
  RESERVED_HOST_PORTS["$chosen"]=1
}

# ─── .env bootstrap ──────────────────────────────────────────────────────────

ensure_env_file() {
  if [[ ! -f "$ENV_FILE" ]]; then
    cp "${ROOT}/.env.example" "$ENV_FILE"
    chmod 600 "$ENV_FILE"
    fixed "created .env from .env.example"
  else
    chmod 600 "$ENV_FILE" || true
  fi
  load_env

  # Inject provider keys from the environment (do not invent keys).
  local k
  for k in "${PROVIDER_KEYS[@]}"; do
    if is_real_key "${!k:-}"; then
      upsert_env "$k" "${!k}"
      fixed "wrote ${k} into .env"
    fi
  done
  load_env

  local ok=0
  for k in "${PROVIDER_KEYS[@]}"; do
    if is_real_key "${CFG[$k]:-}"; then
      ok=1
      break
    fi
  done
  [[ "$ok" -eq 1 ]] || die "No valid provider API key in .env. Export one and re-run."

  # Secrets
  local key new val
  for key in LITELLM_MASTER_KEY LITELLM_SALT_KEY LITELLM_UI_PASSWORD LITELLM_DB_PASSWORD \
             POSTGRES_PASSWORD WEBUI_SECRET_KEY WEBUI_ADMIN_PASSWORD; do
    val="${CFG[$key]:-}"
    if [[ -z "$val" || "$val" == "auto" ]]; then
      case "$key" in
        LITELLM_MASTER_KEY) new="$(rand_sk)" ;;
        WEBUI_ADMIN_PASSWORD) new="$(rand_pw)" ;;
        *) new="$(rand_hex)" ;;
      esac
      upsert_env "$key" "$new"
      fixed "generated ${key}"
    fi
  done

  : "${CFG[WEBUI_ADMIN_NAME]:=Admin}"
  : "${CFG[WEBUI_ADMIN_EMAIL]:=admin@localhost}"
  : "${CFG[LITELLM_UI_USERNAME]:=admin}"
  upsert_env "WEBUI_ADMIN_NAME" "${CFG[WEBUI_ADMIN_NAME]}"
  upsert_env "WEBUI_ADMIN_EMAIL" "${CFG[WEBUI_ADMIN_EMAIL]}"
  upsert_env "LITELLM_UI_USERNAME" "${CFG[LITELLM_UI_USERNAME]}"

  # BIND_HOST: keep existing 0.0.0.0; otherwise force localhost
  if [[ "${CFG[BIND_HOST]:-}" == "0.0.0.0" ]]; then
    :
  else
    if [[ "${CFG[BIND_HOST]:-}" != "127.0.0.1" ]]; then
      fixed "BIND_HOST → 127.0.0.1"
    fi
    upsert_env "BIND_HOST" "127.0.0.1"
  fi

  ensure_port_var OPENWEBUI_HOST_PORT 3001
  ensure_port_var LITELLM_HOST_PORT 4000
  ensure_port_var LITELLM_ECOLOGITS_HOST_PORT 4001
  ensure_port_var LITELLM_COST_HOST_PORT 4002
  ensure_port_var POSTGRES_HOST_PORT 5434
  if [[ "$PROFILE" == "complete" ]]; then
    ensure_port_var MCPO_HOST_PORT 8001
    ensure_port_var N8N_HOST_PORT 5679
    ensure_port_var LANGFUSE_HOST_PORT 3002
    local n8n_port="${CFG[N8N_HOST_PORT]}"
    local n8n_url="${CFG[N8N_EDITOR_BASE_URL]:-}"
    if [[ -z "$n8n_url" || "$n8n_url" == http://localhost:* ]]; then
      upsert_env "N8N_EDITOR_BASE_URL" "http://localhost:${n8n_port}"
    fi
    local lf_port="${CFG[LANGFUSE_HOST_PORT]}"
    local lf_url="${CFG[LANGFUSE_URL]:-}"
    if [[ -z "$lf_url" || "$lf_url" == http://localhost:* ]]; then
      upsert_env "LANGFUSE_URL" "http://localhost:${lf_port}"
    fi
  fi
  load_env

  local owui_port="${CFG[OPENWEBUI_HOST_PORT]}"
  local webui_url="${CFG[WEBUI_URL]:-}"
  if [[ -z "$webui_url" || "$webui_url" == http://localhost:* ]]; then
    upsert_env "WEBUI_URL" "http://localhost:${owui_port}"
  fi
  local redir="${CFG[MICROSOFT_REDIRECT_URI]:-}"
  if [[ -z "$redir" || "$redir" == http://localhost:*/oauth/microsoft/callback ]]; then
    upsert_env "MICROSOFT_REDIRECT_URI" "http://localhost:${owui_port}/oauth/microsoft/callback"
  fi
  load_env
}

# ─── runtime litellm config + stable volumes ─────────────────────────────────

# Prefer fixed genaistack_* volume names so a different unzip folder still finds data.
# Only adopt known leftovers from this stack (owui_* / owui_full_*) — never other projects.
pick_volume_name() {
  local logical="$1"
  local preferred="genaistack_${logical}"
  if docker volume inspect "$preferred" >/dev/null 2>&1; then
    echo "$preferred"
    return 0
  fi
  local candidate
  for candidate in "owui_${logical}" "owui_full_${logical}"; do
    if docker volume inspect "$candidate" >/dev/null 2>&1; then
      echo "$candidate"
      return 0
    fi
  done
  echo "$preferred"
}

# Existing volumes from another compose project need external: true.
volume_yaml_block() {
  local logical="$1" name="$2"
  if docker volume inspect "$name" >/dev/null 2>&1; then
    cat <<EOF
  ${logical}:
    name: ${name}
    external: true
EOF
  else
    cat <<EOF
  ${logical}:
    name: ${name}
EOF
  fi
}

write_runtime_litellm_config() {
  # Minimal: lean LiteLLM (no optional callbacks).
  # Complete: keep cost + emissions callbacks (needed by OpenWebUI filters); drop langfuse until keys exist.
  python3 - "${ROOT}/litellm-config.yaml" "$RUNTIME_CFG" "$PROFILE" <<'PY'
import re, sys
from pathlib import Path
src, dst, profile = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
text = src.read_text()
text = re.sub(r"(?m)^(\s*)success_callback:\s*.*$", r"\1success_callback: []", text)
text = re.sub(r"(?m)^(\s*)failure_callback:\s*.*$", r"\1failure_callback: []", text)
if profile == "complete":
    text = re.sub(
        r"(?m)^(\s*)callbacks:\s*.*$",
        r'\1callbacks: ["litellm_cost_callback.proxy_handler_instance", "ecologits_callback.proxy_handler_instance"]',
        text,
    )
else:
    text = re.sub(r"(?m)^(\s*)callbacks:\s*.*$", r"\1callbacks: []", text)
dst.write_text(text)
PY

  local vol_pg vol_owui vol_ollama vol_n8n
  vol_pg="$(pick_volume_name postgres_data)"
  vol_owui="$(pick_volume_name open_webui_data)"
  vol_ollama="$(pick_volume_name ollama_data)"
  vol_n8n="$(pick_volume_name n8n_data)"

  local reused=0
  for pair in "postgres_data:${vol_pg}" "open_webui_data:${vol_owui}"; do
    logical="${pair%%:*}"
    actual="${pair#*:}"
    if docker volume inspect "$actual" >/dev/null 2>&1; then
      if [[ "$actual" == "genaistack_${logical}" || "$actual" == "owui_${logical}" || "$actual" == "owui_full_${logical}" ]]; then
        reused=1
      fi
    fi
  done
  if [[ "$reused" -eq 1 ]]; then
    echo "    reusing existing chat/database volumes where present"
  fi

  {
    cat <<EOF
# Generated by genaistack — do not edit by hand.
services:
  litellm:
    volumes:
      - ./genaistack/litellm-config.runtime.yaml:/app/config.yaml

volumes:
EOF
    volume_yaml_block postgres_data "$vol_pg"
    volume_yaml_block open_webui_data "$vol_owui"
    volume_yaml_block ollama_data "$vol_ollama"
    volume_yaml_block n8n_data "$vol_n8n"
  } > "$COMPOSE_OVERRIDE"
}

# ─── network / leftovers / unhealthy stack ───────────────────────────────────

ensure_network() {
  if docker network inspect ollama_network >/dev/null 2>&1; then
    return 0
  fi
  if docker network create ollama_network >/dev/null 2>&1; then
    fixed "created docker network ollama_network"
    return 0
  fi
  info "Retrying ollama_network create…"
  docker network rm ollama_network >/dev/null 2>&1 || true
  docker network create ollama_network >/dev/null \
    || die "Could not create docker network ollama_network. Free it with: docker network rm ollama_network"
  fixed "recreated docker network ollama_network"
}

# Fixed container_name values from docker-compose.yml — collisions break reinstalls.
ALL_NAMED_CONTAINERS=(
  owui litellm postgres user-sync mcpo n8n_2 ollama_2 langfuse oauth-revocation
)

reconcile_named_containers() {
  local c state health project
  info "Checking for leftover containers from earlier installs…"
  for c in "${ALL_NAMED_CONTAINERS[@]}"; do
    if ! docker inspect "$c" >/dev/null 2>&1; then
      continue
    fi
    state="$(docker inspect "$c" --format '{{.State.Status}}' 2>/dev/null || echo missing)"
    health="$(docker inspect "$c" --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' 2>/dev/null || echo none)"
    # Remove broken / half-created containers; volumes are kept.
    if [[ "$state" == "created" || "$state" == "exited" || "$state" == "dead" \
       || "$state" == "restarting" || "$health" == "unhealthy" ]]; then
      fixed "removing leftover container ${c} (${state}/${health}) — data volumes kept"
      docker rm -f "$c" >/dev/null 2>&1 || true
    fi
  done

  # Interrupted compose renames look like "<hash>_litellm"
  local orphan
  while IFS= read -r orphan; do
    [[ -z "$orphan" ]] && continue
    fixed "removing orphan container ${orphan}"
    docker rm -f "$orphan" >/dev/null 2>&1 || true
  done < <(docker ps -a --format '{{.Names}}' | grep -E '^[a-f0-9]+_(litellm|owui|postgres|user-sync|mcpo)$' || true)
}

stop_optional() {
  info "Stopping services not in this profile…"
  (cd "$ROOT" && docker compose -f docker-compose.yml -f genaistack/docker-compose.genaistack.yml \
    stop "${OPTIONAL_STOP[@]}" 2>/dev/null) || true
  # Also stop by container name if compose project label differs (old folder name).
  docker stop ollama_2 >/dev/null 2>&1 || true
  if [[ "$PROFILE" != "complete" ]]; then
    docker stop mcpo n8n_2 langfuse >/dev/null 2>&1 || true
  fi
}

repair_stack_containers() {
  local c state health
  for c in "${STACK_CONTAINERS[@]}"; do
    if ! docker inspect "$c" >/dev/null 2>&1; then
      continue
    fi
    state="$(docker inspect "$c" --format '{{.State.Status}}' 2>/dev/null || echo missing)"
    health="$(docker inspect "$c" --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' 2>/dev/null || echo none)"
    if [[ "$state" == "exited" || "$state" == "dead" || "$state" == "restarting" \
       || "$state" == "created" || "$health" == "unhealthy" ]]; then
      fixed "recreating unhealthy/exited container ${c} (${state}/${health})"
      docker rm -f "$c" >/dev/null 2>&1 || true
    fi
  done
}

sql_escape() {
  printf '%s' "$1" | sed "s/'/''/g"
}

# Verify DB password over the docker network (scram). Localhost inside the
# postgres container is "trust" in pg_hba.conf, so 127.0.0.1 checks are meaningless.
pg_network_login_ok() {
  local user="$1" pass="$2" db="$3"
  docker run --rm --network ollama_network -e PGPASSWORD="$pass" postgres:15-alpine \
    psql -h postgres -p 5434 -U "$user" -d "$db" -v ON_ERROR_STOP=1 -c 'SELECT 1' >/dev/null 2>&1
}

# Existing postgres volumes ignore new POSTGRES_PASSWORD from .env — sync users to match .env.
ensure_postgres_credentials_and_db() {
  load_env
  local pgpass="${CFG[POSTGRES_PASSWORD]:-}"
  local litellm_pass="${CFG[LITELLM_DB_PASSWORD]:-}"
  [[ -n "$pgpass" && -n "$litellm_pass" ]] || die "POSTGRES_PASSWORD / LITELLM_DB_PASSWORD missing from .env."

  # Socket auth as n8n usually works inside the container even when TCP password is wrong.
  if ! docker exec postgres psql -U n8n -p 5434 -v ON_ERROR_STOP=1 -c 'SELECT 1' >/dev/null 2>&1; then
    die "Postgres is up but local admin login failed. Check: docker logs postgres"
  fi

  if pg_network_login_ok n8n "$pgpass" n8n; then
    echo "    postgres password matches .env"
  else
    fixed "postgres volume password did not match .env — updating roles to match .env (data kept)"
    docker exec postgres psql -U n8n -p 5434 -v ON_ERROR_STOP=1 \
      -c "ALTER USER n8n WITH PASSWORD '$(sql_escape "$pgpass")';" >/dev/null
    pg_network_login_ok n8n "$pgpass" n8n \
      || die "Could not verify n8n database login after password sync."
  fi

  # Ensure litellm role + database exist (init-db.sh only runs on first empty volume).
  local has_user has_db
  has_user="$(docker exec postgres psql -U n8n -p 5434 -tAc "SELECT 1 FROM pg_roles WHERE rolname='litellm'" 2>/dev/null | tr -d '[:space:]' || true)"
  has_db="$(docker exec postgres psql -U n8n -p 5434 -tAc "SELECT 1 FROM pg_database WHERE datname='litellm'" 2>/dev/null | tr -d '[:space:]' || true)"

  if [[ "$has_user" != "1" ]]; then
    fixed "creating missing postgres role litellm"
    docker exec postgres psql -U n8n -p 5434 -v ON_ERROR_STOP=1 \
      -c "CREATE USER litellm WITH PASSWORD '$(sql_escape "$litellm_pass")';" >/dev/null
  else
    docker exec postgres psql -U n8n -p 5434 -v ON_ERROR_STOP=1 \
      -c "ALTER USER litellm WITH PASSWORD '$(sql_escape "$litellm_pass")';" >/dev/null
  fi

  if [[ "$has_db" != "1" ]]; then
    fixed "creating missing database litellm"
    docker exec postgres psql -U n8n -p 5434 -v ON_ERROR_STOP=1 \
      -c "CREATE DATABASE litellm OWNER litellm;" >/dev/null
  fi

  docker exec postgres psql -U n8n -p 5434 -v ON_ERROR_STOP=1 -d litellm <<SQL >/dev/null
GRANT ALL ON SCHEMA public TO litellm;
ALTER SCHEMA public OWNER TO litellm;
SQL

  if ! pg_network_login_ok litellm "$litellm_pass" litellm; then
    die "Could not verify litellm database login after repair."
  fi
  echo "    litellm database login ok"

  if [[ "$PROFILE" == "complete" ]]; then
    local has_lf
    has_lf="$(docker exec postgres psql -U n8n -p 5434 -tAc "SELECT 1 FROM pg_database WHERE datname='langfuse'" 2>/dev/null | tr -d '[:space:]' || true)"
    if [[ "$has_lf" != "1" ]]; then
      fixed "creating missing database langfuse"
      docker exec postgres psql -U n8n -p 5434 -v ON_ERROR_STOP=1 \
        -c "CREATE DATABASE langfuse OWNER n8n;" >/dev/null
    fi
  fi
}

# ─── start services ──────────────────────────────────────────────────────────

wait_postgres() {
  local i stable=0 ready=0
  info "Waiting for postgres…"
  for i in $(seq 1 90); do
    if docker exec postgres psql -U n8n -p 5434 -v ON_ERROR_STOP=1 -c 'SELECT 1' >/dev/null 2>&1; then
      stable=$((stable + 1))
      if [[ "$stable" -ge 3 ]]; then
        echo "    ready (${i}s)"
        ready=1
        break
      fi
    else
      stable=0
    fi
    sleep 1
  done
  [[ "$ready" -eq 1 ]] || {
    docker logs postgres 2>&1 | tail -40 >&2
    die "Postgres did not become ready in time."
  }
  ensure_postgres_credentials_and_db
}


start_stack() {
  unset OPENWEBUI_ADMIN_API_KEY POSTGRES_PASSWORD LITELLM_DB_PASSWORD LITELLM_MASTER_KEY 2>/dev/null || true

  run_quiet "Starting postgres…" compose up -d --remove-orphans postgres \
    || die "Failed to start postgres."
  wait_postgres

  local services=(litellm open-webui user-sync)
  local build_services=("${services[@]}")
  if [[ "$PROFILE" == "complete" ]]; then
    # mcpo/n8n/langfuse are pull-only images.
    services+=(mcpo n8n langfuse)
  fi
  # build has no --no-deps; up does (open-webui depends_on ollama).
  if [[ "$PROFILE" == "complete" ]]; then
    run_quiet "Building images (complete profile can take a few minutes)…" \
      compose build "${build_services[@]}" \
      || die "Image build failed."
  else
    run_quiet "Building images…" \
      compose build "${build_services[@]}" \
      || die "Image build failed."
  fi
  # Pull published images before up --no-build.
  if [[ "$PROFILE" == "complete" ]]; then
    run_quiet "Pulling mcpo n8n langfuse…" \
      compose pull mcpo n8n langfuse \
      || die "Failed to pull mcpo/n8n/langfuse images."
  fi
  run_quiet "Starting ${services[*]}…" \
    compose up -d --remove-orphans --no-deps --no-build "${services[@]}" \
    || die "Failed to start services."

  # Runtime litellm config is bind-mounted but only read at process start.
  # Force recreate so minimal→complete picks up cost/emissions callbacks.
  if [[ "$PROFILE" == "complete" ]]; then
    run_quiet "Recreating litellm (complete callbacks)…" \
      compose up -d --force-recreate --no-deps --no-build litellm \
      || die "Failed to recreate litellm."
  fi
}

# When an existing open_webui volume has an admin but .env has a new password, sync DB → .env.
reset_openwebui_admin_password() {
  local email="$1" password="$2"
  info "Existing OpenWebUI data found, but login failed — resetting admin password to match .env…"
  # Password via env to avoid shell quoting issues; bcrypt hash written into auth table.
  if ! docker exec -i -e OWUI_RESET_EMAIL="$email" -e OWUI_RESET_PASSWORD="$password" owui \
    python - <<'PY'
import os, sqlite3, sys
try:
    import bcrypt
except ImportError:
    sys.exit(2)
email = os.environ["OWUI_RESET_EMAIL"].strip().lower()
password = os.environ["OWUI_RESET_PASSWORD"]
db = "/app/backend/data/webui.db"
conn = sqlite3.connect(db)
conn.row_factory = sqlite3.Row
cur = conn.cursor()
row = cur.execute("SELECT id, email FROM auth WHERE lower(email)=?", (email,)).fetchone()
if row is None:
    row = cur.execute(
        "SELECT a.id, a.email FROM auth a JOIN user u ON u.id=a.id WHERE u.role='admin' LIMIT 1"
    ).fetchone()
if row is None:
    print("no admin auth row found", file=sys.stderr)
    sys.exit(1)
hashed = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
cur.execute("UPDATE auth SET password=?, email=?, active=1 WHERE id=?", (hashed, email, row["id"]))
cur.execute("UPDATE user SET email=? WHERE id=?", (email, row["id"]))
conn.commit()
conn.close()
print(row["id"])
PY
  then
    return 1
  fi
  fixed "reset OpenWebUI admin password for ${email} (data kept)"
  return 0
}

# ─── OpenWebUI admin + default group ─────────────────────────────────────────

provision_admin_and_group() {
  local email password name port base
  email="${CFG[WEBUI_ADMIN_EMAIL]:-}"
  password="${CFG[WEBUI_ADMIN_PASSWORD]:-}"
  name="${CFG[WEBUI_ADMIN_NAME]:-Admin}"
  port="${CFG[OPENWEBUI_HOST_PORT]:-3001}"
  base="http://127.0.0.1:${port}"

  [[ -n "$email" && -n "$password" ]] || die "WEBUI_ADMIN_EMAIL / WEBUI_ADMIN_PASSWORD missing from .env."

  info "Waiting for OpenWebUI at ${base}…"
  local i health elapsed=0
  for i in $(seq 1 90); do
    curl -4 -s --connect-timeout 2 --max-time 5 -o /dev/null "${base}/" || true
    if curl -4 -sf --connect-timeout 2 --max-time 5 "${base}/health" >/dev/null; then
      health="$(docker inspect owui --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' 2>/dev/null || echo none)"
      if [[ "$health" == "healthy" || "$health" == "none" ]]; then
        if [[ -t 1 ]]; then
          printf '\r    ready (%ss).                    \n' "$elapsed"
        else
          echo "    ready (${elapsed}s)"
        fi
        break
      fi
    fi
    if [[ "$i" -eq 90 ]]; then
      if [[ -t 1 ]]; then
        printf '\r    timed out after %ss.            \n' "$elapsed"
      fi
      die "OpenWebUI not reachable at ${base}/health."
    fi
    if [[ -t 1 ]]; then
      printf '\r    waiting… %ss   ' "$elapsed"
    elif (( i % 6 == 0 )); then
      echo "    still starting (${elapsed}s)…"
    fi
    sleep 5
    elapsed=$((elapsed + 5))
  done

  local work resp cfg_json http token api_key body user_id
  work="$(mktemp -d)"
  chmod 700 "$work"
  resp="${work}/resp.json"
  cfg_json="${work}/admin-config.json"
  body="${work}/body.json"
  printf '%s\n%s\n%s\n' "$name" "$email" "$password" > "${work}/account"
  chmod 600 "${work}/account"
  python3 - "${work}/account" "$body" "${work}/signin.json" <<'PY'
import json, sys
name, email, password = open(sys.argv[1]).read().split("\n", 2)
password = password[:-1] if password.endswith("\n") else password
json.dump({"name": name, "email": email, "password": password}, open(sys.argv[2], "w"))
json.dump({"email": email, "password": password}, open(sys.argv[3], "w"))
PY

  owui_curl() {
    local method="$1" url="$2" outfile="$3" datafile="${4:-}"
    local cfg="${work}/curl.cfg"
    umask 077
    {
      printf 'header = "Content-Type: application/json"\n'
      if [[ -n "${token:-}" ]]; then
        printf 'header = "Authorization: Bearer %s"\n' "$token"
      fi
    } > "$cfg"
    if [[ -n "$datafile" ]]; then
      curl -4 -sS --connect-timeout 3 --max-time 30 --config "$cfg" \
        -o "$outfile" -w '%{http_code}' -X "$method" --data-binary "@${datafile}" "$url"
    else
      curl -4 -sS --connect-timeout 3 --max-time 30 --config "$cfg" \
        -o "$outfile" -w '%{http_code}' -X "$method" "$url"
    fi
  }

  info "Ensuring admin account (${email})…"
  http=$(owui_curl POST "${base}/api/v1/auths/signup" "$resp" "$body")
  if [[ "$http" != "200" ]]; then
    echo "    signup skipped (HTTP ${http}) — signing in…"
    http=$(owui_curl POST "${base}/api/v1/auths/signin" "$resp" "${work}/signin.json")
    if [[ "$http" != "200" ]]; then
      # Typical after reusing open_webui_data with a freshly generated .env password.
      if reset_openwebui_admin_password "$email" "$password"; then
        http=$(owui_curl POST "${base}/api/v1/auths/signin" "$resp" "${work}/signin.json")
      fi
    fi
    if [[ "$http" != "200" ]]; then
      echo "Sign-in response:" >&2
      head -c 500 "$resp" >&2 || true
      echo >&2
      die "Sign-in failed (HTTP ${http}). Your chat data volume may not match this .env. Fix WEBUI_ADMIN_EMAIL/PASSWORD or re-run after restoring the previous .env."
    fi
  else
    echo "    created admin via signup"
  fi

  token=$(python3 - "$resp" <<'PY'
import json, sys
print(json.load(open(sys.argv[1])).get("token", ""))
PY
)
  [[ -n "$token" ]] || die "No JWT token returned from OpenWebUI."

  user_id=$(python3 - "$resp" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
print(d.get("id") or (d.get("user") or {}).get("id") or "")
PY
)

  info "Enabling API keys and closing public signup…"
  http=$(owui_curl GET "${base}/api/v1/auths/admin/config" "$cfg_json")
  [[ "$http" == "200" ]] || die "Failed to read admin config (HTTP ${http})."
  python3 - "$cfg_json" <<'PY'
import json, sys
p = sys.argv[1]
c = json.load(open(p))
c["ENABLE_API_KEYS"] = True
c["ENABLE_SIGNUP"] = False
json.dump(c, open(p, "w"))
PY
  http=$(owui_curl POST "${base}/api/v1/auths/admin/config" "$resp" "$cfg_json")
  [[ "$http" == "200" ]] || die "Failed to update admin config (HTTP ${http})."

  if [[ -n "${CFG[OPENWEBUI_ADMIN_API_KEY]:-}" ]]; then
    # After --wipe-data, volumes are new but .env keeps the old key — verify before trusting it.
    http=$(curl -4 -sS --connect-timeout 3 --max-time 15 \
      -o /dev/null -w '%{http_code}' \
      -H "Authorization: Bearer ${CFG[OPENWEBUI_ADMIN_API_KEY]}" \
      "${base}/api/v1/users/" || echo 000)
    if [[ "$http" == "200" ]]; then
      info "OPENWEBUI_ADMIN_API_KEY already present — keeping it"
    else
      fixed "OPENWEBUI_ADMIN_API_KEY invalid (HTTP ${http}) — regenerating"
      upsert_env "OPENWEBUI_ADMIN_API_KEY" ""
      CFG[OPENWEBUI_ADMIN_API_KEY]=""
    fi
  fi

  if [[ -z "${CFG[OPENWEBUI_ADMIN_API_KEY]:-}" ]]; then
    info "Creating admin API key…"
    http=$(owui_curl POST "${base}/api/v1/auths/api_key" "$resp")
    if [[ "$http" != "200" ]]; then
      http=$(owui_curl GET "${base}/api/v1/auths/api_key" "$resp")
      [[ "$http" == "200" ]] || die "API key create/get failed (HTTP ${http})."
    fi
    api_key=$(python3 - "$resp" <<'PY'
import json, sys
print(json.load(open(sys.argv[1])).get("api_key", ""))
PY
)
    [[ -n "$api_key" ]] || die "No api_key in response."
    upsert_env "OPENWEBUI_ADMIN_API_KEY" "$api_key"
    fixed "wrote OPENWEBUI_ADMIN_API_KEY to .env"
  fi

  # Seed default group
  info "Ensuring OpenWebUI group 'default'…"
  http=$(owui_curl GET "${base}/api/v1/groups/" "$resp")
  [[ "$http" == "200" ]] || die "Failed to list groups (HTTP ${http})."

  local group_id
  group_id=$(python3 - "$resp" <<'PY'
import json, sys
groups = json.load(open(sys.argv[1]))
if isinstance(groups, dict):
    groups = groups.get("items") or groups.get("groups") or []
for g in groups or []:
    if g.get("name") == "default":
        print(g.get("id", ""))
        break
PY
)

  if [[ -z "$group_id" ]]; then
    printf '%s\n' '{"name":"default","description":"Default group created by genaistack installer"}' > "${work}/group.json"
    http=$(owui_curl POST "${base}/api/v1/groups/create" "$resp" "${work}/group.json")
    [[ "$http" == "200" ]] || die "Failed to create default group (HTTP ${http})."
    group_id=$(python3 - "$resp" <<'PY'
import json, sys
print(json.load(open(sys.argv[1])).get("id", ""))
PY
)
    [[ -n "$group_id" ]] || die "default group created but no id returned."
    fixed "created OpenWebUI group 'default'"
  else
    echo "    group 'default' already exists (${group_id})"
  fi

  if [[ -n "$user_id" ]]; then
    printf '{"user_ids":["%s"]}\n' "$user_id" > "${work}/add_users.json"
    http=$(owui_curl POST "${base}/api/v1/groups/id/${group_id}/users/add" "$resp" "${work}/add_users.json")
    if [[ "$http" == "200" ]]; then
      fixed "added admin to group 'default'"
    else
      # Older OpenWebUI: update group with user_ids
      printf '{"name":"default","description":"Default group created by genaistack installer","user_ids":["%s"]}\n' \
        "$user_id" > "${work}/group_update.json"
      http=$(owui_curl POST "${base}/api/v1/groups/id/${group_id}/update" "$resp" "${work}/group_update.json")
      [[ "$http" == "200" ]] || die "Failed to add admin to default group (HTTP ${http})."
      fixed "added admin to group 'default' via update"
    fi
  else
    echo "WARNING: could not determine admin user id; add the admin to group 'default' in the UI." >&2
  fi

  rm -rf "$work"

  info "Provisioning models for group 'default'…"
  unset OPENWEBUI_ADMIN_API_KEY
  run_quiet "Recreating user-sync…" compose up -d --force-recreate --no-deps user-sync \
    || die "Failed to recreate user-sync."

  if [[ "$PROFILE" == "complete" ]]; then
    install_openwebui_functions "$base" "$token"
    ensure_mcpo_tool_server "$base" "$token"
  fi
}

# Install functions/*.py into OpenWebUI (complete profile). Idempotent: skip if id exists.
install_openwebui_functions() {
  local base="$1" token="$2"
  local func_dir="${ROOT}/functions"
  [[ -d "$func_dir" ]] || {
    echo "WARNING: functions/ folder missing — skipping function install." >&2
    return 0
  }

  info "Installing OpenWebUI functions from functions/…"
  local f id name content_file http list_file existing
  list_file="$(mktemp)"
  curl -4 -sS --connect-timeout 3 --max-time 30 \
    -H "Authorization: Bearer ${token}" \
    -o "$list_file" "${base}/api/v1/functions/" >/dev/null || true

  for f in "${func_dir}"/*.py; do
    [[ -f "$f" ]] || continue
    # tr -c leaves a trailing '_' from the newline; strip edges.
    id="$(basename "$f" .py | tr '[:upper:]' '[:lower:]' | tr -c 'a-z0-9_' '_' | sed 's/^_*//;s/_*$//')"
    name="$(basename "$f" .py)"
    existing="$(python3 - "$list_file" "$id" <<'PY'
import json, sys
path, want = sys.argv[1], sys.argv[2]
try:
    data = json.load(open(path))
except Exception:
    data = []
if isinstance(data, dict):
    data = data.get("items") or data.get("functions") or []
for fn in data or []:
    if (fn.get("id") or "").lower() == want:
        print("1")
        break
PY
)"
    if [[ "$existing" == "1" ]]; then
      echo "    function '${id}' already present — keeping it"
      continue
    fi

    content_file="$(mktemp)"
    python3 - "$f" "$id" "$name" "$content_file" <<'PY'
import json, sys
from pathlib import Path
src, fid, name, out = Path(sys.argv[1]), sys.argv[2], sys.argv[3], Path(sys.argv[4])
content = src.read_text()
payload = {
    "id": fid,
    "name": name,
    "content": content,
    "meta": {"description": f"Installed by genaistack from functions/{src.name}", "manifest": {}},
}
out.write_text(json.dumps(payload))
PY
    http=$(curl -4 -sS --connect-timeout 3 --max-time 60 \
      -o /tmp/genaistack-fn-resp.json -w '%{http_code}' \
      -H "Authorization: Bearer ${token}" \
      -H "Content-Type: application/json" \
      -X POST --data-binary @"${content_file}" \
      "${base}/api/v1/functions/create")
    rm -f "$content_file"
    if [[ "$http" == "200" ]]; then
      fixed "installed function ${id}"
      # Enable it (toggle from inactive default)
      curl -4 -sS --connect-timeout 3 --max-time 30 \
        -H "Authorization: Bearer ${token}" \
        -X POST "${base}/api/v1/functions/id/${id}/toggle" >/dev/null || true
    else
      echo "WARNING: could not install function ${id} (HTTP ${http}). Configure it manually in Admin → Functions." >&2
    fi
  done
  rm -f "$list_file"

  # Drop functions we no longer ship, plus earlier installs that used trailing '_' ids.
  local stale_id
  for stale_id in image_generation image_generation_ \
      cost_display_ emissions_display_ n8n_pipe_; do
    http=$(curl -4 -sS --connect-timeout 3 --max-time 30 \
      -o /dev/null -w '%{http_code}' \
      -H "Authorization: Bearer ${token}" \
      -X DELETE "${base}/api/v1/functions/id/${stale_id}/delete" 2>/dev/null || echo 000)
    if [[ "$http" == "200" ]]; then
      fixed "removed deprecated function ${stale_id}"
    fi
  done

  # Filters only run on chats when active + global (otherwise admin must attach per model).
  ensure_function_flags "$base" "$token" cost_display filter
  ensure_function_flags "$base" "$token" emissions_display filter
  ensure_function_flags "$base" "$token" n8n_pipe pipe
}

# Ensure is_active (and for filters, is_global) so installed functions actually run.
ensure_function_flags() {
  local base="$1" token="$2" id="$3" kind="$4"
  local info_file http active global
  info_file="$(mktemp)"
  http=$(curl -4 -sS --connect-timeout 3 --max-time 30 \
    -o "$info_file" -w '%{http_code}' \
    -H "Authorization: Bearer ${token}" \
    "${base}/api/v1/functions/id/${id}" 2>/dev/null || echo 000)
  if [[ "$http" != "200" ]]; then
    rm -f "$info_file"
    return 0
  fi
  active="$(python3 - "$info_file" <<'PY'
import json, sys
print("1" if json.load(open(sys.argv[1])).get("is_active") else "0")
PY
)"
  global="$(python3 - "$info_file" <<'PY'
import json, sys
print("1" if json.load(open(sys.argv[1])).get("is_global") else "0")
PY
)"
  rm -f "$info_file"
  if [[ "$active" != "1" ]]; then
    curl -4 -sS --connect-timeout 3 --max-time 30 \
      -H "Authorization: Bearer ${token}" \
      -X POST "${base}/api/v1/functions/id/${id}/toggle" >/dev/null || true
    fixed "enabled function ${id}"
  fi
  if [[ "$kind" == "filter" && "$global" != "1" ]]; then
    curl -4 -sS --connect-timeout 3 --max-time 30 \
      -H "Authorization: Bearer ${token}" \
      -X POST "${base}/api/v1/functions/id/${id}/toggle/global" >/dev/null || true
    fixed "made filter ${id} global (runs on all models)"
  fi
}

# Register mcpo (mcp-server-fetch) as a global OpenAPI tool server in OpenWebUI.
ensure_mcpo_tool_server() {
  local base="$1" token="$2"
  local conn_id="mcpo_fetch"
  # OpenWebUI backend reaches mcpo on the docker network (not via published host port).
  local mcpo_url="http://mcpo:8000"
  local list_file out_file http changed
  local mcpo_host_port

  info "Ensuring OpenWebUI tool server 'Web Fetch' (mcpo)…"
  load_env
  mcpo_host_port="${CFG[MCPO_HOST_PORT]:-8001}"

  # Installer uses --network host, so "mcpo" DNS does not resolve here.
  # Probe via published localhost port; still register the in-network URL for owui.
  if ! curl -4 -sf --connect-timeout 3 --max-time 10 \
      "http://127.0.0.1:${mcpo_host_port}/openapi.json" >/dev/null \
    && ! docker exec owui python -c "import urllib.request; urllib.request.urlopen('${mcpo_url}/openapi.json', timeout=5)" >/dev/null 2>&1; then
    echo "WARNING: mcpo OpenAPI not reachable — skip tool registration." >&2
    return 0
  fi

  list_file="$(mktemp)"
  out_file="$(mktemp)"
  curl -4 -sS --connect-timeout 3 --max-time 30 \
    -H "Authorization: Bearer ${token}" \
    -o "$list_file" "${base}/api/v1/configs/tool_servers" >/dev/null || true

  changed="$(python3 - "$list_file" "$out_file" "$conn_id" "$mcpo_url" <<'PY'
import json, sys
src, dst, conn_id, url = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
try:
    data = json.load(open(src))
except Exception:
    data = {}
conns = list(data.get("TOOL_SERVER_CONNECTIONS") or [])
wanted = {
    "url": url,
    "path": "openapi.json",
    "type": "openapi",
    "auth_type": "none",
    "key": "",
    "config": {"enable": True},
    "info": {
        "id": conn_id,
        "name": "Web Fetch",
        "description": "Fetch URLs from the internet (mcpo mcp-server-fetch)",
    },
}
changed = False
found = False
for i, c in enumerate(conns):
    cid = ((c.get("info") or {}).get("id") or "")
    if cid == conn_id or (c.get("url") or "").rstrip("/") == url.rstrip("/"):
        found = True
        merged = dict(c)
        merged["url"] = wanted["url"]
        merged["path"] = wanted["path"]
        merged["type"] = wanted["type"]
        merged["auth_type"] = wanted.get("auth_type", merged.get("auth_type") or "none")
        cfg = dict(merged.get("config") or {})
        cfg["enable"] = True
        merged["config"] = cfg
        info = dict(merged.get("info") or {})
        info.update(wanted["info"])
        merged["info"] = info
        if merged != c:
            conns[i] = merged
            changed = True
        break
if not found:
    conns.append(wanted)
    changed = True
open(dst, "w").write(json.dumps({"TOOL_SERVER_CONNECTIONS": conns}))
print("1" if changed else "0")
PY
)"

  if [[ "$changed" != "1" ]]; then
    echo "    tool server '${conn_id}' already configured — keeping it"
    rm -f "$list_file" "$out_file"
    return 0
  fi

  http=$(curl -4 -sS --connect-timeout 3 --max-time 30 \
    -o /tmp/genaistack-tool-servers.json -w '%{http_code}' \
    -H "Authorization: Bearer ${token}" \
    -H "Content-Type: application/json" \
    -X POST --data-binary @"$out_file" \
    "${base}/api/v1/configs/tool_servers")
  rm -f "$list_file" "$out_file"
  if [[ "$http" == "200" ]]; then
    fixed "registered global tool server Web Fetch (mcpo)"
  else
    echo "WARNING: could not register mcpo tool server (HTTP ${http}). Add it in Admin → Settings → Tools." >&2
  fi
}

# ─── post-start health ───────────────────────────────────────────────────────

check_health() {
  local port base
  load_env
  port="${CFG[OPENWEBUI_HOST_PORT]}"
  base="http://127.0.0.1:${port}"

  info "Post-start health checks…"

  docker exec postgres psql -U n8n -p 5434 -v ON_ERROR_STOP=1 -c 'SELECT 1' >/dev/null \
    || die "Postgres health check failed."

  curl -4 -sf --connect-timeout 3 --max-time 10 "${base}/health" >/dev/null \
    || die "OpenWebUI health check failed at ${base}/health."

  local litellm_port="${CFG[LITELLM_HOST_PORT]}"
  # /health requires an API key (401); use /health/liveliness and wait —
  # complete profile callbacks make LiteLLM slower to become ready.
  local i ready=0
  echo -n "    waiting for LiteLLM on port ${litellm_port}"
  for i in $(seq 1 90); do
    if curl -4 -sf --connect-timeout 2 --max-time 5 \
        "http://127.0.0.1:${litellm_port}/health/liveliness" >/dev/null; then
      echo " — ready (${i}s)"
      ready=1
      break
    fi
    echo -n "."
    sleep 1
  done
  [[ "$ready" -eq 1 ]] || {
    echo
    docker logs litellm 2>&1 | tail -40 >&2
    die "LiteLLM health check failed on port ${litellm_port}."
  }

  if [[ "$PROFILE" == "complete" ]]; then
    local mcpo_port="${CFG[MCPO_HOST_PORT]:-8001}"
    local n8n_port="${CFG[N8N_HOST_PORT]:-5679}"
    local lf_port="${CFG[LANGFUSE_HOST_PORT]:-3002}"
    local i ready=0
    curl -4 -s --connect-timeout 3 --max-time 10 -o /dev/null \
      "http://127.0.0.1:${mcpo_port}/" \
      || die "mcpo did not respond on port ${mcpo_port}."
    # n8n may still be running DB migrations after first start.
    echo -n "    waiting for n8n on port ${n8n_port}"
    ready=0
    for i in $(seq 1 90); do
      if curl -4 -s --connect-timeout 2 --max-time 5 -o /dev/null \
          "http://127.0.0.1:${n8n_port}/"; then
        echo " — ready (${i}s)"
        ready=1
        break
      fi
      echo -n "."
      sleep 1
    done
    [[ "$ready" -eq 1 ]] || {
      echo
      docker logs n8n_2 2>&1 | tail -40 >&2
      die "n8n did not respond on port ${n8n_port}."
    }
    echo -n "    waiting for langfuse on port ${lf_port}"
    ready=0
    for i in $(seq 1 90); do
      if curl -4 -s --connect-timeout 2 --max-time 5 -o /dev/null \
          "http://127.0.0.1:${lf_port}/api/public/health" \
        || curl -4 -s --connect-timeout 2 --max-time 5 -o /dev/null \
          "http://127.0.0.1:${lf_port}/"; then
        echo " — ready (${i}s)"
        ready=1
        break
      fi
      echo -n "."
      sleep 1
    done
    [[ "$ready" -eq 1 ]] || {
      echo
      docker logs langfuse 2>&1 | tail -40 >&2
      die "langfuse did not respond on port ${lf_port}."
    }
  fi

  load_env
  [[ -n "${CFG[OPENWEBUI_ADMIN_API_KEY]:-}" ]] || die "OPENWEBUI_ADMIN_API_KEY is empty after provision."

  # Confirm default group still present
  local work http token
  work="$(mktemp -d)"
  chmod 700 "$work"
  printf '%s\n' "{\"email\":\"${CFG[WEBUI_ADMIN_EMAIL]}\",\"password\":\"${CFG[WEBUI_ADMIN_PASSWORD]}\"}" > "${work}/signin.json"
  http=$(curl -4 -sS --connect-timeout 3 --max-time 30 \
    -o "${work}/resp.json" -w '%{http_code}' \
    -H 'Content-Type: application/json' \
    -X POST --data-binary @"${work}/signin.json" \
    "${base}/api/v1/auths/signin")
  [[ "$http" == "200" ]] || die "Could not sign in to verify default group (HTTP ${http})."
  token=$(python3 - "${work}/resp.json" <<'PY'
import json, sys
print(json.load(open(sys.argv[1])).get("token", ""))
PY
)
  http=$(curl -4 -sS --connect-timeout 3 --max-time 30 \
    -o "${work}/groups.json" -w '%{http_code}' \
    -H "Authorization: Bearer ${token}" \
    "${base}/api/v1/groups/")
  [[ "$http" == "200" ]] || die "Could not list groups for verification (HTTP ${http})."
  python3 - "${work}/groups.json" <<'PY' || die "default group missing after install."
import json, sys
groups = json.load(open(sys.argv[1]))
if isinstance(groups, dict):
    groups = groups.get("items") or groups.get("groups") or []
assert any(g.get("name") == "default" for g in (groups or [])), "no default group"
PY
  rm -rf "$work"
  echo "    all health checks passed"
}

print_summary() {
  load_env
  local owui="${CFG[OPENWEBUI_HOST_PORT]}"
  local litellm="${CFG[LITELLM_HOST_PORT]}"
  echo
  echo "========================================"
  echo "  Installation finished"
  echo "========================================"
  echo
  echo "Open this in your browser:"
  echo "  http://localhost:${owui}"
  echo
  echo "Sign in with:"
  echo "  Email:    ${CFG[WEBUI_ADMIN_EMAIL]}"
  echo "  Password: (see WEBUI_ADMIN_PASSWORD in the .env file in this folder)"
  echo
  echo "Advanced UI (optional): http://localhost:${litellm}/ui"
  if [[ "$PROFILE" == "complete" ]]; then
    echo "Tools proxy (mcpo):     http://localhost:${CFG[MCPO_HOST_PORT]:-8001}"
    echo "n8n:                    http://localhost:${CFG[N8N_HOST_PORT]:-5679}"
    echo "Langfuse:               http://localhost:${CFG[LANGFUSE_HOST_PORT]:-3002}"
    echo
    echo "OpenWebUI functions from functions/ were installed (Admin → Functions)."
    echo "Cost/emissions filters are global; they need a chat turn after LiteLLM is up."
    echo "Web Fetch (mcpo) is registered as a global tool — enable it in the chat"
    echo "  Integrations (+) menu before the model can use it."
    echo "For n8n_pipe: select it as a model, set the webhook URL valve after you create a workflow."
    echo "For Langfuse: open the URL above, create a project, put the API keys in .env."
    echo "  (LiteLLM→Langfuse tracing can be wired in a later install step.)"
  fi
  echo
  echo "You can change settings later in the .env file."
  echo "Running ./install.sh again repairs problems and does not delete your data."
}

write_install_state() {
  local version="" state_file="${ROOT}/.genaistack-state"
  if [[ -f "${ROOT}/VERSION" ]]; then
    version="$(tr -d '[:space:]' < "${ROOT}/VERSION")"
  fi
  version="${version:-unknown}"
  cat > "$state_file" <<EOF
profile=${PROFILE}
level=$([[ "$PROFILE" == "complete" ]] && echo 2 || echo 1)
version=${version}
installed_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
EOF
  info "Wrote install state (${PROFILE}, version=${version})"
}

# Installer container runs as root; hand files back to the host user.
fix_host_ownership() {
  local uid="${HOST_UID:-}" gid="${HOST_GID:-}"
  [[ -n "$uid" && -n "$gid" ]] || return 0
  [[ "$uid" =~ ^[0-9]+$ && "$gid" =~ ^[0-9]+$ ]] || return 0
  chown "$uid:$gid" "$ENV_FILE" 2>/dev/null || true
  chown "$uid:$gid" "$RUNTIME_CFG" "$COMPOSE_OVERRIDE" 2>/dev/null || true
  chown "$uid:$gid" "${ROOT}/.genaistack-state" 2>/dev/null || true
  chmod 600 "$ENV_FILE" 2>/dev/null || true
}

# ─── main ────────────────────────────────────────────────────────────────────

info "genaistack profile=${PROFILE} home=${ROOT}"
echo "STAGE: prepare"
trap fix_host_ownership EXIT
ensure_provider_key
ensure_env_file
echo "STAGE: configure"
write_runtime_litellm_config
ensure_network
reconcile_named_containers
stop_optional
echo "STAGE: repair"
repair_stack_containers
echo "STAGE: start"
start_stack
load_env
echo "STAGE: provision"
provision_admin_and_group
echo "STAGE: health"
check_health
write_install_state
echo "STAGE: done"
print_summary
