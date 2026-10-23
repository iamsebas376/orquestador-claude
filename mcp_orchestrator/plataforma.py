"""Todo lo que depende del sistema operativo (Windows / PowerShell) en un solo lugar.

- Rutas y nombres de binarios por defecto.
- Abrir una ventana de terminal externa con un comando (Windows Terminal o consola de Windows).
- Armar líneas de comando para PowerShell que Claude ejecuta en su panel de terminal.
"""

from __future__ import annotations

import shutil
import subprocess
import unicodedata
from pathlib import Path

IS_WINDOWS = True

REPO_ROOT = Path(__file__).resolve().parent.parent  # carpeta del repositorio (logs, tareas, config)


def exe(name: str) -> str:
    """Nombre de un ejecutable en Windows (agrega .exe si no lo tiene)."""
    return name if name.lower().endswith(".exe") else f"{name}.exe"


def default_dir(nombre: str) -> Path:
    """Carpetas de proyectos por defecto: ~/workspaces, ~/personal, ~/universidad (se cambian en .env)."""
    return Path.home() / nombre


def default_opencode_config() -> Path:
    base = Path.home() / ".config" / "opencode"
    for name in ("opencode.jsonc", "opencode.json"):
        if (base / name).is_file():
            return base / name
    return base / "opencode.jsonc"


def default_agy() -> str:
    return str(Path.home() / ".gemini" / "bin" / exe("agy"))


# ----------------------------------------------------------------------------- terminal externa

def open_terminal(title: str, argv: list[str], cwd: Path, env: dict | None = None) -> str:
    """Abre una ventana de terminal que ejecuta `argv` en `cwd`. Devuelve "" o un mensaje de error."""
    try:
        if wt := shutil.which("wt.exe"):
            subprocess.Popen([wt, "-w", "new", "--title", title, "-d", str(cwd), *argv], env=env)
        else:
            subprocess.Popen(argv, cwd=str(cwd), env=env, creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))
        return ""
    except OSError as e:
        return str(e)


# ----------------------------------------------------------------------------- comandos para el panel de Claude

def _ps(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def quote_path(p: str | Path) -> str:
    return _ps(str(p))


def safe_prompt(text: str) -> str:
    """Prompt en una sola línea, solo ASCII imprimible y sin caracteres que PowerShell interprete.

    El panel de terminal de Claude solo acepta ASCII literal; PowerShell lo recibe entre comillas dobles
    (sin $, `, ")."""
    text = unicodedata.normalize("NFKD", text.replace("ñ", "n").replace("Ñ", "N"))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.replace("¿", "").replace("¡", "").replace("$", "USD ").replace("`", "'").replace('"', "'")
    text = text.encode("ascii", "ignore").decode()
    return " ".join(text.split()).replace(";", ",")


def quote_prompt(text: str) -> str:
    """`text` ya pasado por safe_prompt, entre comillas dobles para PowerShell."""
    return f'"{text}"'


def run_line(program: str | Path, *args: str, cwd: str | Path | None = None) -> str:
    """Una línea de comando para PowerShell: [entrar a cwd y] ejecutar program con args ya citados."""
    call = f"& {quote_path(program)}" + "".join(f" {a}" for a in args)
    if cwd is None:
        return call
    return f"Set-Location {quote_path(cwd)}; {call}"
