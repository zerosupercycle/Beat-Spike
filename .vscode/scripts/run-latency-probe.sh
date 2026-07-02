#!/usr/bin/env bash
# Unzip and run the Polymarket latency probe for the current OS.
# Results are written to data/latency-probe.txt
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BIN_DIR="$ROOT/bin"
EXTRACT_DIR="$BIN_DIR/.extracted"
OUT_DIR="$ROOT/data"
OUT_FILE="$OUT_DIR/latency-probe.txt"

mkdir -p "$EXTRACT_DIR" "$OUT_DIR"

if [[ -f "$OUT_FILE" ]] && find "$OUT_FILE" -mmin -5 -print -quit 2>/dev/null | grep -q .; then
  echo "Skipping latency probe; recent results exist at $OUT_FILE"
  exit 0
fi

detect_platform() {
  case "$(uname -s)" in
    Linux*) echo "linux" ;;
    Darwin*) echo "macos" ;;
    MINGW*|MSYS*|CYGWIN*) echo "windows" ;;
    *) return 1 ;;
  esac
}

PLATFORM="$(detect_platform)" || {
  echo "Unsupported OS for latency probe: $(uname -s)" >&2
  exit 1
}

case "$PLATFORM" in
  linux)
    ZIP="polymarket-latency-probe-linux-x86_64.zip"
    BINARY="polymarket-latency-probe-linux-x86_64"
    ;;
  macos)
    ZIP="polymarket-latency-probe-macos-arm64.zip"
    BINARY="polymarket-latency-probe-macos-arm64"
    ;;
  windows)
    echo "Use run-latency-probe.ps1 on Windows." >&2
    exit 1
    ;;
esac

EXTRACT_PLATFORM_DIR="$EXTRACT_DIR/$PLATFORM"
mkdir -p "$EXTRACT_PLATFORM_DIR"

ZIP_PATH="$BIN_DIR/$ZIP"
PROBE="$EXTRACT_PLATFORM_DIR/$BINARY"

if [[ ! -f "$PROBE" ]]; then
  if [[ ! -f "$ZIP_PATH" ]]; then
    echo "Missing probe archive: $ZIP_PATH" >&2
    exit 1
  fi
  unzip -qo -o "$ZIP_PATH" -d "$EXTRACT_PLATFORM_DIR"
fi

chmod +x "$PROBE"

TIMESTAMP="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
HOST="$(uname -n 2>/dev/null || hostname)"

{
  echo "Polymarket Latency Probe"
  echo "Generated: $TIMESTAMP"
  echo "Platform: $PLATFORM"
  echo "Binary: $BINARY"
  echo "Host: $HOST"
  echo "---"
  "$PROBE" --json -q 2>/dev/null
} > "$OUT_FILE.tmp"

mv "$OUT_FILE.tmp" "$OUT_FILE"
echo "Latency results saved to $OUT_FILE"
