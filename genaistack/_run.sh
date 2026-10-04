#!/usr/bin/env bash
# Shared host runner for genaistack install scripts.
# Usage: sourced by install-minimal.sh / install-complete.sh with GENAISTACK_PROFILE set.
set -euo pipefail

PROFILE="${GENAISTACK_PROFILE:-}"
if [[ "$PROFILE" != "minimal" && "$PROFILE" != "complete" ]]; then
  echo "GENAISTACK_PROFILE must be minimal or complete (got: ${PROFILE:-empty})." >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
IMAGE_NAME="${GENAISTACK_IMAGE:-genaistack-installer}"

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker is required. Install Docker, start it, then re-run this script." >&2
  exit 1
fi
if ! docker info >/dev/null 2>&1; then
  echo "Docker is installed but not running (or this user cannot reach it)." >&2
  echo "Start Docker Desktop / the docker daemon, or add your user to the docker group." >&2
  exit 1
fi

echo "STAGE: build"
echo "==> Building ${IMAGE_NAME}…"
# Quiet by default. On BuildKit cache corruption ("parent snapshot … does not exist"),
# prune builder cache once and retry — do not touch service Dockerfiles.
build_installer() {
  docker build -q -t "$IMAGE_NAME" -f "${SCRIPT_DIR}/Dockerfile" "$ROOT" >/dev/null
}
if ! build_log="$(build_installer 2>&1)"; then
  if echo "$build_log" | grep -qiE 'parent snapshot|does not exist|failed to prepare extraction'; then
    echo "    Docker build cache looks corrupted — clearing builder cache and retrying…"
    docker builder prune -af >/dev/null 2>&1 || true
    if ! build_log="$(build_installer 2>&1)"; then
      echo "$build_log" >&2
      echo "Failed to build ${IMAGE_NAME} after cache reset." >&2
      echo "Try: docker builder prune -af && ./install.sh" >&2
      exit 1
    fi
  else
    echo "$build_log" >&2
    echo "Failed to build ${IMAGE_NAME}." >&2
    exit 1
  fi
fi
echo "    installer image ready"

PROVIDER_ENV_ARGS=()
for k in OPENAI_API_KEY ANTHROPIC_API_KEY AZURE_OPENAI_API_KEY MISTRAL_API_KEY GEMINI_API_KEY; do
  if [[ -n "${!k:-}" ]]; then
    PROVIDER_ENV_ARGS+=(-e "$k")
  fi
done

TTY_ARGS=(-i)
if [[ -t 0 && -t 1 ]]; then
  TTY_ARGS=(-it)
fi

echo "==> Starting genaistack installer (profile=${PROFILE})…"
# shellcheck disable=SC2086
exec docker run --rm "${TTY_ARGS[@]}" --network host \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v "${ROOT}:${ROOT}" \
  -e "GENAISTACK_HOME=${ROOT}" \
  -e "GENAISTACK_PROFILE=${PROFILE}" \
  -e "HOST_UID=$(id -u)" \
  -e "HOST_GID=$(id -g)" \
  ${PROVIDER_ENV_ARGS[@]+"${PROVIDER_ENV_ARGS[@]}"} \
  "$IMAGE_NAME"
