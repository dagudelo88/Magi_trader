#!/usr/bin/env bash
# MagiTrader — install Node + Python dependencies (Linux / macOS).
# Usage:  chmod +x scripts/setup.sh && ./scripts/setup.sh

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

echo "MagiTrader setup — repo: $REPO_ROOT"

for cmd in node npm; do
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "error: '$cmd' not found on PATH (install Node.js LTS)" >&2
    exit 1
  fi
done

PY=""
for candidate in python3 python; do
  if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c "import sys; sys.exit(0)" >/dev/null 2>&1; then
    PY="$candidate"
    break
  fi
done

if [[ -z "$PY" ]]; then
  echo "error: Python 3 not found (install python3)" >&2
  exit 1
fi

echo ""
echo "[1/3] npm ci (repo root)…"
npm ci

echo ""
echo "[2/3] npm ci (frontend, legacy-peer-deps)…"
npm ci --prefix frontend --legacy-peer-deps

echo ""
echo "[3/3] pip install backend/requirements.txt…"
"$PY" -m pip install -r "$REPO_ROOT/backend/requirements.txt"

echo ""
echo "Done. Copy .env if needed, then: npm run dev"
