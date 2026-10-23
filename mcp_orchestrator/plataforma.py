"""Todo lo que depende del sistema operativo (Windows / PowerShell) en un solo lugar.

- Rutas y nombres de binarios por defecto.
"""

from __future__ import annotations

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


# ----------------------------------------------------------------------------- comandos para las pestañas de Claude

def _ps(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def quote_path(p: str | Path) -> str:
    return _ps(str(p))


def quote_prompt(text: str) -> str:
    """Texto ASCII entre comillas dobles para PowerShell (el panel de terminal de Claude solo admite ASCII)."""
    return f'"{text}"'


def run_line(program: str | Path, *args: str, cwd: str | Path | None = None) -> str:
    """Una línea de comando para PowerShell: [entrar a cwd y] ejecutar program con args ya citados."""
    call = f"& {quote_path(program)}" + "".join(f" {a}" for a in args)
    if cwd is None:
        return call
    return f"Set-Location {quote_path(cwd)}; {call}"
