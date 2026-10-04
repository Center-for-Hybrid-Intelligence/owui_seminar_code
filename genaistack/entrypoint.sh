#!/usr/bin/env bash
# genaistack installer entrypoint — preflight, then run setup.sh against GENAISTACK_HOME.
set -euo pipefail

die() { echo "ERROR: $*" >&2; exit 1; }
info() { echo "==> $*"; }
fixed() { echo "fixed: $*"; }

: "${GENAISTACK_HOME:?GENAISTACK_HOME must be set to the unzipped repo path on the host.}"
: "${GENAISTACK_PROFILE:?GENAISTACK_PROFILE must be minimal or complete.}"

[[ "$GENAISTACK_PROFILE" == "minimal" || "$GENAISTACK_PROFILE" == "complete" ]] \
  || die "GENAISTACK_PROFILE must be minimal or complete (got: ${GENAISTACK_PROFILE})."

[[ -d "$GENAISTACK_HOME" ]] || die "GENAISTACK_HOME is not a directory: ${GENAISTACK_HOME}"
[[ -w "$GENAISTACK_HOME" ]] || die "GENAISTACK_HOME is not writable: ${GENAISTACK_HOME}"
[[ -f "${GENAISTACK_HOME}/docker-compose.yml" ]] \
  || die "GENAISTACK_HOME does not look like the stack root (missing docker-compose.yml)."
[[ -f "${GENAISTACK_HOME}/.env.example" ]] \
  || die "GENAISTACK_HOME is missing .env.example."

info "Checking Docker socket…"
docker info >/dev/null 2>&1 || die "Cannot talk to Docker via /var/run/docker.sock. Is Docker running on the host?"
docker compose version >/dev/null 2>&1 || die "Docker Compose v2 plugin is required (docker compose)."

info "Checking host can see GENAISTACK_HOME at the same path…"
marker="${GENAISTACK_HOME}/.genaistack-mount-check"
printf 'ok\n' > "$marker"
chmod 600 "$marker"
if ! docker run --rm -v "${GENAISTACK_HOME}:${GENAISTACK_HOME}:ro" alpine:3.20 \
    cat "${marker}" >/dev/null 2>&1; then
  rm -f "$marker"
  die "Host Docker cannot read ${GENAISTACK_HOME} at the same path. Remount with -v \"\$ROOT:\$ROOT\"."
fi
rm -f "$marker"

info "Checking free disk space…"
avail_kb="$(df -Pk "$GENAISTACK_HOME" | awk 'NR==2 {print $4}')"
if [[ -z "$avail_kb" || ! "$avail_kb" =~ ^[0-9]+$ ]]; then
  die "Could not determine free disk space for ${GENAISTACK_HOME}."
fi
# ~8 GiB
if (( avail_kb < 8 * 1024 * 1024 )); then
  die "Need at least ~8 GB free on ${GENAISTACK_HOME} (have $(( avail_kb / 1024 / 1024 )) GB)."
fi

info "Preflight ok. Starting setup…"
SETUP="${GENAISTACK_HOME}/genaistack/setup.sh"
if [[ -f "$SETUP" ]]; then
  chmod +x "$SETUP" 2>/dev/null || true
  exec "$SETUP"
fi
# Fallback if the zip is incomplete but the image still has a copy
exec /opt/genaistack/setup.sh
