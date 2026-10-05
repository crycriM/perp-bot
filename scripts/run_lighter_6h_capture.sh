#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

exec "$repo_root/.venv/bin/python" scripts/capture_lighter_lob.py ETH BTC SOL --duration 21600
