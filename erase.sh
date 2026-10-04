#!/usr/bin/env bash
# Tear down this stack safely.
#
# Default: stop/remove containers and locally built images. Keeps volumes (.env, chat, DB).
# Data wipe is opt-in:  ./erase.sh --wipe-data
#
# Usage:
#   ./erase.sh
#   ./erase.sh --yes
#   ./erase.sh --wipe-data
#   ./erase.sh --yes --wipe-data --also-network
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
YES=0
WIPE_DATA=0
ALSO_NETWORK=0

usage() {
  cat <<'EOF'
Tear down this OpenWebUI / genaistack install.

Usage:
  ./erase.sh                         # containers + built images (keeps data volumes)
  ./erase.sh --wipe-data             # also delete chat/DB volumes (destructive)
  ./erase.sh --yes                   # skip interactive confirm (still keeps volumes)
  ./erase.sh --yes --wipe-data       # non-interactive full wipe of stack volumes
  ./erase.sh --also-network          # also remove docker network ollama_network

Safe by default: volumes named genaistack_*, owui_*, owui_full_* are kept unless
you pass --wipe-data. .env and pulled images (postgres, n8n, mcpo, …) are always kept.
EOF
}

for arg in "$@"; do
  case "$arg" in
    -y|--yes) YES=1 ;;
    --wipe-data|--wipe-volumes) WIPE_DATA=1 ;;
    --also-network) ALSO_NETWORK=1 ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $arg" >&2
      usage >&2
      exit 1
      ;;
  esac
done

STACK_CONTAINERS=(
  owui litellm langfuse postgres n8n_2 ollama_2 mcpo user-sync docling
  oauth-revocation grafana prometheus blackbox node-exporter
  postgres-exporter cadvisor
)

list_stack_volumes() {
  docker volume ls -q | grep -E '^(genaistack_|owui_|owui_full_)' || true
}

list_stack_containers() {
  local name
  for name in "${STACK_CONTAINERS[@]}"; do
    if docker ps -a --format '{{.Names}}' 2>/dev/null | grep -qx "$name"; then
      echo "$name"
    fi
  done
}

list_built_images() {
  docker images --format '{{.Repository}}:{{.Tag}}' 2>/dev/null \
    | grep -E '^(owui|owui_full|genaistack-installer)(-|$)|^owui-' || true
}

mapfile -t PLAN_CONTAINERS < <(list_stack_containers)
mapfile -t PLAN_VOLUMES < <(list_stack_volumes)
mapfile -t PLAN_IMAGES < <(list_built_images)

echo "Erase plan for: $ROOT"
echo
echo "Will remove containers (${#PLAN_CONTAINERS[@]}):"
if [[ ${#PLAN_CONTAINERS[@]} -eq 0 ]]; then
  echo "  (none)"
else
  printf '  - %s\n' "${PLAN_CONTAINERS[@]}"
fi
echo
echo "Will remove locally built images (${#PLAN_IMAGES[@]}):"
if [[ ${#PLAN_IMAGES[@]} -eq 0 ]]; then
  echo "  (none)"
else
  printf '  - %s\n' "${PLAN_IMAGES[@]}"
fi
echo
if [[ "$WIPE_DATA" -eq 1 ]]; then
  echo "Will DELETE data volumes (${#PLAN_VOLUMES[@]}) — chat history, databases, n8n data:"
  if [[ ${#PLAN_VOLUMES[@]} -eq 0 ]]; then
    echo "  (none found)"
  else
    printf '  - %s\n' "${PLAN_VOLUMES[@]}"
  fi
else
  echo "Will KEEP data volumes (${#PLAN_VOLUMES[@]}) — pass --wipe-data to delete them:"
  if [[ ${#PLAN_VOLUMES[@]} -eq 0 ]]; then
    echo "  (none found)"
  else
    printf '  - %s\n' "${PLAN_VOLUMES[@]}"
  fi
fi
echo
echo "Always kept: .env, pulled images (postgres/n8n/mcpo/open-webui/…)"
if [[ "$ALSO_NETWORK" -eq 1 ]]; then
  echo "Also: docker network ollama_network"
else
  echo "Kept: docker network ollama_network (pass --also-network to remove)"
fi
echo

if [[ "$YES" -ne 1 ]]; then
  if [[ "$WIPE_DATA" -eq 1 ]]; then
    read -r -p "Type 'erase data' to permanently delete volumes and tear down: " answer
    if [[ "$answer" != "erase data" ]]; then
      echo "Aborted."
      exit 1
    fi
  else
    read -r -p "Type 'erase' to continue (volumes kept): " answer
    if [[ "$answer" != "erase" ]]; then
      echo "Aborted."
      exit 1
    fi
  fi
elif [[ "$WIPE_DATA" -eq 1 ]]; then
  echo "==> --yes --wipe-data: proceeding with volume deletion."
fi

echo "==> Stopping compose projects (containers; volumes left unless --wipe-data)…"
for dir in "$ROOT" "${ROOT}/../owui_full"; do
  if [[ -f "${dir}/docker-compose.yml" ]]; then
    echo "    compose down: $dir"
    # Never pass -v here — external genaistack_* volumes must only go via --wipe-data.
    (cd "$dir" && docker compose down --remove-orphans 2>/dev/null) || true
  fi
done

echo "==> Removing leftover containers…"
if [[ ${#PLAN_CONTAINERS[@]} -eq 0 ]]; then
  echo "    (none)"
else
  for name in "${PLAN_CONTAINERS[@]}"; do
    echo "    rm -f $name"
    docker rm -f "$name" >/dev/null 2>&1 || true
  done
fi

if [[ "$WIPE_DATA" -eq 1 ]]; then
  echo "==> Removing data volumes…"
  # Re-scan in case compose created/renamed anything.
  mapfile -t VOLS < <(list_stack_volumes)
  if [[ ${#VOLS[@]} -eq 0 ]]; then
    echo "    (none)"
  else
    for v in "${VOLS[@]}"; do
      # Skip volumes still attached to a running container (safety).
      users="$(docker ps -a --filter "volume=$v" --format '{{.Names}}' 2>/dev/null || true)"
      if [[ -n "$users" ]]; then
        echo "    SKIP $v (still attached to: $(echo "$users" | tr '\n' ' '))"
        continue
      fi
      echo "    volume rm $v"
      docker volume rm "$v" >/dev/null 2>&1 || {
        echo "    WARNING: could not remove $v" >&2
      }
    done
  fi
else
  echo "==> Keeping data volumes (use --wipe-data to delete)."
fi

echo "==> Removing locally built images…"
declare -A SEEN=()
if [[ ${#PLAN_IMAGES[@]} -eq 0 ]]; then
  echo "    (none)"
else
  for img in "${PLAN_IMAGES[@]}"; do
    [[ -z "$img" || "$img" == *"<none>"* ]] && continue
    [[ -n "${SEEN[$img]:-}" ]] && continue
    SEEN[$img]=1
    echo "    rmi $img"
    docker rmi -f "$img" >/dev/null 2>&1 || true
  done
fi

if [[ "$ALSO_NETWORK" -eq 1 ]]; then
  echo "==> Removing network ollama_network…"
  docker network rm ollama_network 2>/dev/null || echo "    (missing or still in use)"
fi

echo "==> Done."
if [[ "$WIPE_DATA" -eq 1 ]]; then
  echo "    Stack containers and data volumes removed."
else
  echo "    Containers removed; data volumes kept. Reinstall will reuse them."
fi
echo "    Reinstall with: ${ROOT}/install.sh"
