#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

# Only install development dependencies; project data and model credentials are not required.
export RADAR_OFFLINE=1
export RADAR_LLM_DAILY_BUDGET_USD=0 RADAR_LLM_CAMPAIGN_BUDGET_USD=0 RADAR_LLM_FILING_BUDGET_USD=0
command -v git >/dev/null
uv sync --frozen --all-extras --python 3.12
uv run --frozen python -c "import radar, sys; assert sys.version_info[:2] == (3, 12); print('WattHorizon Python 3.12 dependencies ready (no collection or model call)')"
if command -v node >/dev/null; then node --version; else echo 'Node missing: JavaScript verification unavailable' >&2; fi
if ! command -v tesseract >/dev/null || ! tesseract --list-langs 2>/dev/null | grep -qx fra; then
  echo 'Host French OCR unavailable; tools/cloud_pilot.py build prepares Tesseract French in Docker.' >&2
fi
