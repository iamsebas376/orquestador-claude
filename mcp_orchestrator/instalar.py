"""Pasos de instalación en Windows (los llama setup.ps1, ya con el venv creado).

1. Crea .env desde .env.example si no existe y genera OPENCODE_SERVER_PASSWORD si está vacía.
2. Registra el servidor MCP en Claude Desktop y en Claude Code (todas las sesiones), con respaldo previo.

Uso: <python del venv> instalar.py [--sin-registro]
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import sys
import tempfile
from pathlib import Path

import plataforma

APP = Path(__file__).resolve().parent
SERVER = APP / "server.py"
PY = Path(sys.executable)


def preparar_env() -> None:
    env, ejemplo = APP / ".env", APP / ".env.example"
    if not env.exists():
        shutil.copy2(ejemplo, env)
        print(f"    {env} creado desde .env.example")
    texto = env.read_text(encoding="utf-8")
    if re.search(r"^OPENCODE_SERVER_PASSWORD=\s*$", texto, flags=re.M):
        texto = re.sub(r"^OPENCODE_SERVER_PASSWORD=\s*$", f"OPENCODE_SERVER_PASSWORD={secrets.token_urlsafe(24)}",
                       texto, count=1, flags=re.M)
        env.write_text(texto, encoding="utf-8")
        print("    contraseña del servidor OpenCode generada")
    for carpeta in ("logs", "tareas"):
        (plataforma.DATOS_DIR / carpeta).mkdir(parents=True, exist_ok=True)


def _escribir_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent))
    with os.fdopen(fd, "w", encoding="utf-8") as fh:  # UTF-8 sin BOM (Claude Desktop no acepta BOM)
        json.dump(data, fh, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def _leer_json(path: Path) -> dict:
    if not path.exists():
        return {}
    shutil.copy2(path, path.with_name(path.name + ".bak"))
    return json.loads(path.read_text(encoding="utf-8-sig") or "{}")


def config_claude_desktop() -> Path:
    return Path(os.environ["APPDATA"]) / "Claude" / "claude_desktop_config.json"


def registrar() -> None:
    desktop = config_claude_desktop()
    d = _leer_json(desktop)
    d.setdefault("mcpServers", {})["orchestrator"] = {"command": str(PY), "args": [str(SERVER)]}
    _escribir_json(desktop, d)
    print(f"    Claude Desktop: {desktop}")

    code = Path.home() / ".claude.json"
    c = _leer_json(code)
    c.setdefault("mcpServers", {})["orchestrator"] = {"type": "stdio", "command": str(PY), "args": [str(SERVER)],
                                                      "env": {}}
    _escribir_json(code, c)
    print(f"    Claude Code (todas las sesiones): {code}")


if __name__ == "__main__":
    print("==> Configuración (.env)")
    preparar_env()
    if "--sin-registro" not in sys.argv:
        print("==> Registro del servidor MCP")
        registrar()
