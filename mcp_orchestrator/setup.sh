#!/usr/bin/env bash
# Instalación del Orquestador MCP en macOS (y Linux).
# Uso (desde la carpeta del repo):  bash mcp_orchestrator/setup.sh [--install-opencode] [--skip-register]
set -euo pipefail

APP="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$APP/.venv"
PY="$VENV/bin/python"
INSTALL_OPENCODE=0
SKIP_REGISTER=0
for arg in "$@"; do
  case "$arg" in
    --install-opencode) INSTALL_OPENCODE=1 ;;
    --skip-register) SKIP_REGISTER=1 ;;
    *) echo "Opción desconocida: $arg" >&2; exit 1 ;;
  esac
done

c() { printf '\033[36m%s\033[0m\n' "$1"; }
y() { printf '\033[33m%s\033[0m\n' "$1"; }

c "==> Entorno virtual + dependencias"
if [ ! -x "$PY" ]; then
  command -v python3 >/dev/null || { echo "Necesitas Python 3.10+ (brew install python)" >&2; exit 1; }
  python3 -m venv "$VENV"
fi
"$PY" -m pip install --upgrade pip --quiet
"$PY" -m pip install -r "$APP/requirements.txt" --quiet
"$PY" -c "import mcp, httpx, dotenv, rich; print('    dependencias OK')"

c "==> OpenCode"
if command -v opencode >/dev/null; then
  echo "    encontrado: $(command -v opencode)"
elif [ "$INSTALL_OPENCODE" = 1 ]; then
  if command -v npm >/dev/null; then npm install -g opencode-ai
  else y "    Necesitas Node.js (brew install node) o instala OpenCode según https://opencode.ai"; fi
else
  y "    NO encontrado. Ejecuta de nuevo con --install-opencode o fija OPENCODE_CLI_PATH en .env"
fi

c "==> Antigravity (agy)"
if [ -x "$HOME/.gemini/bin/agy" ] || command -v agy >/dev/null; then
  echo "    encontrado"
else
  y "    NO encontrado: instala Antigravity e inicia sesión una vez (crea ~/.gemini/bin/agy)"
fi

if [ "$SKIP_REGISTER" = 1 ]; then "$PY" "$APP/instalar.py" --sin-registro; else "$PY" "$APP/instalar.py"; fi

printf '\n\033[32mListo. Pendiente:\033[0m\n'
echo "  1. Revisa $APP/.env (carpetas, modelos, PANEL_URL) y guarda la clave de respaldo con set-key.sh si la usas"
echo "  2. Copia config/CLAUDE.example.md a ~/.claude/CLAUDE.md (Code) y config/PROJECT_INSTRUCTIONS.md a tu Proyecto (Chat)"
echo "  3. Reinicia Claude Desktop"
