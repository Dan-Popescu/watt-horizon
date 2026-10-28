#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

# Only install development dependencies; project data and model credentials are not required.
export RADAR_OFFLINE=1
uv sync --frozen --all-extras
uv run python -c "import radar; print('WattHorizon development environment ready')"
