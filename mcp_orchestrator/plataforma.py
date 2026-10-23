"""Todo lo que depende del sistema operativo (Windows / PowerShell) en un solo lugar.

- Rutas y nombres de binarios por defecto.
"""

from __future__ import annotations

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
