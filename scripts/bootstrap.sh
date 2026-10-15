#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
python3 -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate
pip install -U pip
pip install -e ".[dev]"
echo "==> running smoke suite"
ticketbench-run smoke --out harness/report/smoke_last.json
echo "==> bootstrap OK"
