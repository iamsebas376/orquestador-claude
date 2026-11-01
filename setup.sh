#!/usr/bin/env bash
# Envoltorio raíz hacia mcp_orchestrator/setup.sh
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
exec "$ROOT/mcp_orchestrator/setup.sh" "$@"
