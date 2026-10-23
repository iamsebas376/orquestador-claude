"""Todo lo que depende del sistema operativo (Windows / PowerShell) en un solo lugar.

- Rutas y nombres de binarios por defecto.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent  # carpeta del repositorio
DATOS_DIR = REPO_ROOT / "datos"  # todo lo que genera el orquestador: logs, pedidos, costos, aprendizajes


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


def open_terminal(title: str, argv: list[str], cwd: Path, env: dict | None = None) -> str:
    """Abre una ventana de terminal (Windows Terminal o consola de Windows) que ejecuta `argv` en `cwd`.
    Devuelve "" o un mensaje de error."""
    try:
        if wt := shutil.which("wt.exe"):
            subprocess.Popen([wt, "-w", "new", "--title", title, "-d", str(cwd), *argv], env=env)
        else:
            subprocess.Popen(argv, cwd=str(cwd), env=env, creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))
        return ""
    except OSError as e:
        return str(e)
