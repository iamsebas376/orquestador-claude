#!/usr/bin/env bash
# Guarda la API key de Gemini (modo api de respaldo) en .env sin mostrarla en pantalla ni en el historial.
# Uso: bash mcp_orchestrator/set-key.sh
set -euo pipefail
APP="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$APP/.env"
[ -f "$ENV_FILE" ] || cp "$APP/.env.example" "$ENV_FILE"

read -r -s -p "Pega tu API key de Google AI Studio: " KEY; echo
[ -n "$KEY" ] || { echo "Clave vacía, no se cambió nada."; exit 1; }

KEY="$KEY" "$APP/.venv/bin/python" - "$ENV_FILE" <<'PY'
import os, re, sys
p = sys.argv[1]
t = open(p, encoding="utf-8").read()
k = os.environ["KEY"].strip()
t = re.sub(r"^ANTIGRAVITY_API_KEY=.*$", lambda m: "ANTIGRAVITY_API_KEY=" + k, t, flags=re.M) \
    if re.search(r"^ANTIGRAVITY_API_KEY=", t, flags=re.M) else t + f"\nANTIGRAVITY_API_KEY={k}\n"
open(p, "w", encoding="utf-8").write(t)
PY

if curl -fsS -H "Authorization: Bearer $KEY" https://generativelanguage.googleapis.com/v1beta/openai/models >/dev/null; then
  echo "OK: la clave funciona. Reinicia Claude Desktop."
else
  echo "La clave se guardó, pero la prueba falló."
fi
