#!/usr/bin/env bash
# GenAI Stack — company-friendly installer.
# Default: open a local browser GUI. Use --cli for the terminal menu.
# Unzip the project, then run:  ./install.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GENAI="${ROOT}/genaistack"
UI_SERVER="${ROOT}/installer-ui/server.py"

if [[ ! -f "${GENAI}/_run.sh" ]]; then
  echo "This folder looks incomplete (missing genaistack/). Unzip the full package and try again." >&2
  exit 1
fi

cli_install() {
  if [[ ! -t 0 || ! -t 1 ]]; then
    echo "CLI mode needs a normal terminal window:" >&2
    echo "  ./install.sh --cli" >&2
    exit 1
  fi

  echo
  echo "========================================"
  echo "  GenAI Stack installer (CLI)"
  echo "========================================"
  echo
  echo "You need:"
  echo "  • Linux"
  echo "  • Docker installed and running"
  echo "  • An API key from your AI provider (OpenAI, Anthropic, …)"
  echo
  echo "What should we install?"
  echo "  1) Standard (recommended) — chat ready to use"
  echo "  2) Complete — chat + mcpo + n8n + langfuse + OpenWebUI functions"
  echo
  read -r -p "Choice [1]: " choice
  choice="${choice:-1}"

  case "$choice" in
    1) export GENAISTACK_PROFILE=minimal ;;
    2) export GENAISTACK_PROFILE=complete ;;
    *)
      echo "Please answer 1 or 2." >&2
      exit 1
      ;;
  esac

  echo
  echo "Next you will be asked for your AI provider and API key."
  echo "The key is saved only in a local .env file on this computer."
  echo
  # shellcheck source=genaistack/_run.sh
  source "${GENAI}/_run.sh"
}

gui_install() {
  if ! command -v python3 >/dev/null 2>&1; then
    echo "Python 3 is required for the graphical installer." >&2
    echo "Install python3, or run:  ./install.sh --cli" >&2
    exit 1
  fi
  if [[ ! -f "$UI_SERVER" ]]; then
    echo "Missing installer UI (${UI_SERVER}). Try ./install.sh --cli" >&2
    exit 1
  fi

  echo
  echo "Opening the GenAI Stack installer in your browser…"
  echo "Leave this terminal open until you are finished."
  echo
  exec python3 "$UI_SERVER"
}

case "${1:-}" in
  --cli|-c)
    cli_install
    ;;
  --help|-h)
    echo "Usage: ./install.sh [--cli]"
    echo "  (default)  Open the graphical installer in your browser"
    echo "  --cli      Terminal menu (SSH / automation)"
    ;;
  "")
    gui_install
    ;;
  *)
    echo "Unknown option: $1" >&2
    echo "Usage: ./install.sh [--cli]" >&2
    exit 1
    ;;
esac
