"""Todo lo que depende del sistema operativo (Windows / macOS / Linux) en un solo lugar.

- Detección de SO, rutas por defecto, búsqueda de binarios y generación de comandos para terminal.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent  # carpeta del repositorio
DATOS_DIR = Path(os.getenv("DATOS_DIR") or REPO_ROOT / "datos").expanduser()  # logs, pedidos, costos, aprendizajes


def es_windows() -> bool:
    return sys.platform == "win32"


def es_macos() -> bool:
    return sys.platform == "darwin"


def exe(name: str) -> str:
    """Nombre de un ejecutable según SO: en Windows agrega .exe si no tiene extensión (.exe/.cmd/.bat/.ps1); en POSIX lo deja igual."""
    if not es_windows():
        return name[:-4] if name.lower().endswith(".exe") else name
    if name.lower().endswith((".exe", ".cmd", ".bat", ".ps1")):
        return name
    return f"{name}.exe"


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


def _npm_global_bin() -> list[Path]:
    """Rutas probables de binarios globales de npm sin bloquear ni fallar."""
    rutas: list[Path] = []
    home = Path.home()
    if es_windows():
        appdata = os.environ.get("APPDATA")
        if appdata:
            rutas.append(Path(appdata) / "npm")
        rutas.append(home / "AppData" / "Roaming" / "npm")
    else:
        rutas.extend([
            home / ".npm-global" / "bin",
            home / ".nvm" / "current" / "bin",
            home / ".volta" / "bin",
            home / ".fnm" / "current" / "bin",
        ])
    try:
        res = subprocess.run(["npm", "prefix", "-g"], capture_output=True, text=True, timeout=2, stdin=subprocess.DEVNULL)
        if res.returncode == 0 and res.stdout.strip():
            pref = Path(res.stdout.strip())
            rutas.append(pref if es_windows() else pref / "bin")
    except (OSError, subprocess.SubprocessError):
        pass
    return rutas


def buscar_agy(preferido: str = "") -> str | None:
    """Localiza el binario `agy` (Antigravity CLI) en la ruta indicada, PATH o ubicaciones típicas de Windows/macOS."""
    if preferido:
        p = Path(preferido).expanduser()
        if p.is_file():
            return str(p)
        found = shutil.which(preferido)
        if found:
            return found

    found = shutil.which("agy") or (shutil.which("agy.exe") if es_windows() else None)
    if found:
        return found

    home = Path.home()
    bin_name = exe("agy")
    candidatos = [home / ".gemini" / "bin" / bin_name]
    if not es_windows():
        candidatos.extend([
            home / ".gemini" / "bin" / "agy",
            Path("/opt/homebrew/bin/agy"),
            Path("/usr/local/bin/agy"),
            home / ".local" / "bin" / "agy",
        ])
    for c in candidatos:
        if c.is_file():
            return str(c)
    return None


def buscar_opencode(preferido: str = "") -> str | None:
    """Localiza el binario `opencode` en la ruta indicada, PATH, tools/opencode del repo o rutas típicas de macOS/Windows."""
    if preferido and preferido != "opencode":
        p = Path(preferido).expanduser()
        if p.is_file():
            return str(p)
        found = shutil.which(preferido)
        if found:
            return found

    for nombre in (("opencode.exe", "opencode.cmd", "opencode") if es_windows() else ("opencode",)):
        found = shutil.which(nombre)
        if found:
            return found

    home = Path.home()
    tools_oc = REPO_ROOT / "tools" / "opencode"
    candidatos: list[Path] = []
    if es_windows():
        candidatos.extend([
            tools_oc / "node_modules" / "opencode-ai" / "node_modules" / "opencode-windows-x64" / "bin" / "opencode.exe",
            tools_oc / "node_modules" / "opencode-ai" / "bin" / "opencode.exe",
            tools_oc / "opencode.cmd",
            home / ".opencode" / "bin" / "opencode.exe",
        ])
        for nb in _npm_global_bin():
            candidatos.extend([nb / "opencode.cmd", nb / "opencode.exe"])
    else:
        candidatos.extend([
            home / ".opencode" / "bin" / "opencode",
            Path("/opt/homebrew/bin/opencode"),
            Path("/usr/local/bin/opencode"),
            home / ".local" / "bin" / "opencode",
            tools_oc / "node_modules" / ".bin" / "opencode",
            tools_oc / "opencode",
        ])
        for nb in _npm_global_bin():
            candidatos.append(nb / "opencode")

    for c in candidatos:
        if c.is_file():
            return str(c)
    return None


def config_claude_desktop() -> Path:
    """Ruta de claude_desktop_config.json según el sistema operativo."""
    if es_windows():
        base = Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming"))
        return base / "Claude" / "claude_desktop_config.json"
    if es_macos():
        return Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    xdg = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    return xdg / "Claude" / "claude_desktop_config.json"


def config_claude_code() -> Path:
    """Ruta de configuración global de Claude Code (~/.claude.json)."""
    return Path.home() / ".claude.json"


def claude_dir() -> Path:
    """Carpeta ~/.claude (CLAUDE.md, settings.json, projects)."""
    return Path.home() / ".claude"


def default_claude_app_dir() -> Path:
    """Carpeta de datos de sesiones de la app de Claude según SO."""
    if es_windows():
        return Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")) / "Claude-Data"
    if es_macos():
        app_supp = Path.home() / "Library" / "Application Support"
        if (app_supp / "Claude-Data").exists():
            return app_supp / "Claude-Data"
        return app_supp / "Claude"
    xdg = Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share"))
    return xdg / "Claude-Data"


def venv_python(venv_dir: Path | None = None) -> Path:
    """Ejecutable de Python dentro del entorno virtual (.venv)."""
    v = venv_dir or (REPO_ROOT / "mcp_orchestrator" / ".venv")
    return v / "Scripts" / "python.exe" if es_windows() else v / "bin" / "python"


# ----------------------------------------------------------------------------- comandos para las pestañas de Claude

def _ps(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def quote_path(p: str | Path) -> str:
    """Cita una ruta de forma segura según el shell del SO (PowerShell en Windows, POSIX en macOS/Linux)."""
    s = str(p)
    return _ps(s) if es_windows() else shlex.quote(s)


def quote_prompt(text: str) -> str:
    """Cita un prompt para línea de comandos según SO (en Windows entre comillas dobles para PowerShell; en POSIX con shlex.quote)."""
    if es_windows():
        return f'"{text}"'
    return shlex.quote(text)


def _quote_arg_posix(a: str) -> str:
    if not a:
        return "''"
    if (a.startswith("'") and a.endswith("'")) or (a.startswith('"') and a.endswith('"')):
        return a
    return shlex.quote(a)


def run_line(program: str | Path, *args: str, cwd: str | Path | None = None) -> str:
    """Una línea de comando para run_in_terminal:
    - Windows (PowerShell): [Set-Location 'cwd'; ] & 'program' args...
    - macOS/Linux (POSIX): [cd 'cwd' && ] 'program' args...
    """
    if es_windows():
        call = f"& {quote_path(program)}" + "".join(f" {a}" for a in args)
        if cwd is None:
            return call
        return f"Set-Location {quote_path(cwd)}; {call}"
    prog_q = shlex.quote(str(program))
    args_q = "".join(f" {_quote_arg_posix(str(a))}" for a in args)
    call = f"{prog_q}{args_q}"
    if cwd is None:
        return call
    return f"cd {shlex.quote(str(cwd))} && {call}"
