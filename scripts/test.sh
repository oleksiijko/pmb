#!/usr/bin/env bash
# Run the test suite.
#
#   bash scripts/test.sh                # the whole suite (CI-equivalent)
#   bash scripts/test.sh tests/recall   # one directory
#   bash scripts/test.sh -k dashboard   # one keyword
#   bash scripts/test.sh tests/integration/test_dashboard.py -q
#
# Arguments pass through to pytest. The default includes every blocking test,
# including property and MCP integration tests; install .[dev,crypto] first.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

if [ "$#" -gt 0 ]; then
  exec python -m pytest -q "$@"
fi

exec python -m pytest -q -m "not quarantined"
