"""Todo lo que depende del sistema operativo (Windows, macOS, Linux) en un solo lugar.

- Rutas y nombres de binarios por defecto.
- Abrir una ventana de terminal externa con un comando (Windows Terminal, Terminal.app/iTerm, Linux).
- Armar líneas de comando para el shell del usuario (PowerShell en Windows, zsh/bash en macOS/Linux),
  que Claude ejecuta en su panel de terminal.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import unicodedata
from pathlib import Path

IS_WINDOWS = os.name == "nt"
IS_MAC = sys.platform == "darwin"

REPO_ROOT = Path(__file__).resolve().parent.parent  # carpeta del repositorio (logs, tareas, config)


def exe(name: str) -> str:
    """Nombre de un ejecutable en esta plataforma (agrega .exe en Windows)."""
    return f"{name}.exe" if IS_WINDOWS and not name.lower().endswith(".exe") else name


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
        if IS_WINDOWS:
            if wt := shutil.which("wt.exe"):
                subprocess.Popen([wt, "-w", "new", "--title", title, "-d", str(cwd), *argv], env=env)
            else:
                subprocess.Popen(argv, cwd=str(cwd), env=env, creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))
            return ""
        line = f"cd {shlex.quote(str(cwd))} && " + _env_prefix(env) + " ".join(shlex.quote(a) for a in argv)
        if IS_MAC:
            # Terminal.app: abre una ventana nueva que ejecuta la línea en el shell del usuario.
            osa = f'tell application "Terminal"\n  do script {_applescript_string(line)}\n  activate\nend tell'
            subprocess.Popen(["osascript", "-e", osa])
            return ""
        for term, flag in (("x-terminal-emulator", "-e"), ("gnome-terminal", "--"), ("konsole", "-e"), ("xterm", "-e")):
            if shutil.which(term):
                subprocess.Popen([term, flag, "bash", "-lc", line + "; exec bash"])
                return ""
        return "no se encontró un emulador de terminal"
    except OSError as e:
        return str(e)


def _env_prefix(env: dict | None) -> str:
    """En macOS/Linux la ventana nueva no hereda el entorno: pasa solo las variables del orquestador."""
    if not env:
        return ""
    keep = {k: v for k, v in env.items() if k.startswith(("OPENCODE_", "ORQ_"))}
    return "".join(f"{k}={shlex.quote(v)} " for k, v in keep.items())


def _applescript_string(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


# ----------------------------------------------------------------------------- comandos para el panel de Claude

def _ps(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def quote_path(p: str | Path) -> str:
    return _ps(str(p)) if IS_WINDOWS else shlex.quote(str(p))


def safe_prompt(text: str) -> str:
    """Prompt en una sola línea, solo ASCII imprimible y sin caracteres que el shell interprete.

    El panel de terminal de Claude solo acepta ASCII literal; PowerShell lo recibe entre comillas dobles
    (sin $, `, "), y zsh/bash entre comillas simples (sin ')."""
    text = unicodedata.normalize("NFKD", text.replace("ñ", "n").replace("Ñ", "N"))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.replace("¿", "").replace("¡", "").replace("$", "USD ").replace("`", "'").replace('"', "'")
    if not IS_WINDOWS:
        text = text.replace("'", "")
    text = text.encode("ascii", "ignore").decode()
    return " ".join(text.split()).replace(";", ",")


def quote_prompt(text: str) -> str:
    """`text` ya pasado por safe_prompt, entre las comillas que corresponden al shell."""
    return f'"{text}"' if IS_WINDOWS else f"'{text}'"


def run_line(program: str | Path, *args: str, cwd: str | Path | None = None) -> str:
    """Una línea de comando para el shell del usuario: [entrar a cwd y] ejecutar program con args ya citados."""
    call = (f"& {quote_path(program)}" if IS_WINDOWS else quote_path(program)) + "".join(f" {a}" for a in args)
    if cwd is None:
        return call
    return (f"Set-Location {quote_path(cwd)}; " if IS_WINDOWS else f"cd {quote_path(cwd)} && ") + call
