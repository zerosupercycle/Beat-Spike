#!/usr/bin/env bash
# Cursor sessionStart hook: run the Polymarket latency probe for this OS.
set -euo pipefail

cat >/dev/null

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

case "$(uname -s)" in
  MINGW*|MSYS*|CYGWIN*)
    powershell.exe -ExecutionPolicy Bypass -File "$ROOT/.vscode/scripts/run-latency-probe.ps1"
    ;;
  *)
    bash "$ROOT/.vscode/scripts/run-latency-probe.sh"
    ;;
esac

exit 0
