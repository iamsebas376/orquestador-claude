#!/usr/bin/env bash
# Instalación del Orquestador MCP en macOS / Linux (envoltorio fino hacia instalar.py)
# Uso (desde la carpeta del repo):
#   ./mcp_orchestrator/setup.sh [--yes] [--install-opencode] [--skip-register] [--dry-run] [--solo-diagnostico]
set -euo pipefail

APP="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
ROOT="$(cd "$APP/.." && pwd)"
VENV="$APP/.venv"
PY_VENV="$VENV/bin/python"

AUTO_YES=0
INSTALL_OC=0
FORWARD_ARGS=()

for arg in "$@"; do
  case "$arg" in
    --yes|-y)
      AUTO_YES=1
      ;;
    --install-opencode)
      INSTALL_OC=1
      ;;
    --skip-register)
      FORWARD_ARGS+=("--sin-registro")
      ;;
    --dry-run|--solo-diagnostico|--sin-registro|--sin-deps)
      FORWARD_ARGS+=("$arg")
      ;;
    *)
      FORWARD_ARGS+=("$arg")
      ;;
  esac
done

detectar_python() {
  for cand in python3 python /opt/homebrew/bin/python3 /usr/local/bin/python3; do
    if command -v "$cand" >/dev/null 2>&1; then
      if "$cand" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >/dev/null 2>&1; then
        command -v "$cand"
        return 0
      fi
    fi
  done
  return 1
}

SYS_PY=""
if [ -x "$PY_VENV" ]; then
  SYS_PY="$PY_VENV"
elif SYS_PY="$(detectar_python)"; then
  :
else
  echo "⚠ No se encontró Python 3.10+ en el sistema."
  if command -v brew >/dev/null 2>&1; then
    if [ "$AUTO_YES" -eq 1 ]; then
      echo "==> Instalando python@3.12 con Homebrew (--yes)..."
      brew install python@3.12
    else
      printf "¿Deseas instalar Python 3.12 con Homebrew ahora? [s/N]: "
      read -r resp
      if [[ "$resp" =~ ^[sS]([iIíÍ])?$|^y|Y ]]; then
        brew install python@3.12
      else
        echo "Aborta: instala Python 3.10+ manualmente y vuelve a ejecutar setup.sh."
        exit 1
      fi
    fi
    SYS_PY="$(detectar_python)"
  else
    echo "Instala Python 3.10+ (p. ej. desde https://python.org o instalando Homebrew + 'brew install python') y reintenta."
    exit 1
  fi
fi

if [ "$INSTALL_OC" -eq 1 ] && ! command -v opencode >/dev/null 2>&1; then
  echo "==> Instalando OpenCode..."
  if command -v npm >/dev/null 2>&1; then
    OC_DIR="$ROOT/tools/opencode"
    mkdir -p "$OC_DIR"
    npm install opencode-ai --no-save --prefix "$OC_DIR"
  elif command -v brew >/dev/null 2>&1; then
    brew install opencode || true
  fi
fi

exec "$SYS_PY" "$APP/instalar.py" "${FORWARD_ARGS[@]}"
