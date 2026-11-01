"""Instalador multiplataforma e idempotente del Orquestador MCP (Windows / macOS / Linux).

1. Crea .venv (con uv si existe o python -m venv) e instala requirements.txt.
2. Crea .env desde .env.example si no existe y genera OPENCODE_SERVER_PASSWORD si está vacía.
3. Registra el servidor MCP 'orchestrator' en Claude Code (~/.claude.json) y en Claude Desktop.
4. Instala/actualiza el bloque de reglas en ~/.claude/CLAUDE.md entre marcadores sin pisar contenido del usuario.
5. Instala hooks en ~/.claude/settings.json sin pisar otros hooks.
6. Imprime un diagnóstico claro de qué quedó listo y qué falta (sin pedir ni mostrar secretos).

Uso: python instalar.py [--solo-diagnostico] [--dry-run] [--sin-registro] [--sin-deps]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import plataforma

APP = Path(__file__).resolve().parent
SERVER = APP / "server.py"
PY = Path(sys.executable)

MARCADOR_INICIO = "<!-- ORQUESTADOR-CLAUDE:INICIO -->"
MARCADOR_FIN = "<!-- ORQUESTADOR-CLAUDE:FIN -->"

HOOK_CERRAR_PEDIDO = {
    "matcher": "mcp__orchestrator__cerrar_pedido",
    "hooks": [{
        "type": "command",
        "command": (
            'echo \'{"hookSpecificOutput":{"hookEventName":"PostToolUse","additionalContext":'
            '"Pedido cerrado: ahora llama mcp__orchestrator__exportar_panel y ejecuta tal cual '
            'la llamada ArtifactData batch que devuelve, para sincronizar el panel de control."}}\''
        ),
        "timeout": 10,
    }],
}


def asegurar_venv(dry_run: bool = False) -> Path:
    """Asegura que .venv exista y tenga las dependencias instaladas. Devuelve el ejecutable de Python del venv."""
    venv_dir = APP / ".venv"
    py_venv = plataforma.venv_python(venv_dir)
    if dry_run:
        print(f"    [dry-run] entorno virtual en {venv_dir} ({py_venv})")
        return py_venv if py_venv.exists() else Path(sys.executable)

    uv = shutil.which("uv")
    if not py_venv.exists():
        print(f"    creando entorno virtual en {venv_dir}...")
        if uv:
            subprocess.run([uv, "venv", str(venv_dir)], check=True)
        else:
            subprocess.run([sys.executable, "-m", "venv", str(venv_dir)], check=True)

    req = APP / "requirements.txt"
    if req.exists():
        if uv:
            subprocess.run([uv, "pip", "install", "--python", str(py_venv), "-r", str(req), "--quiet"], check=True)
        else:
            subprocess.run([str(py_venv), "-m", "pip", "install", "--upgrade", "pip", "--quiet"], check=True)
            subprocess.run([str(py_venv), "-m", "pip", "install", "-r", str(req), "--quiet"], check=True)
    return py_venv


def preparar_env(dry_run: bool = False) -> None:
    env, ejemplo = APP / ".env", APP / ".env.example"
    if dry_run:
        if not env.exists():
            print(f"    [dry-run] crearía {env} desde .env.example y generaría OPENCODE_SERVER_PASSWORD")
        else:
            print(f"    [dry-run] conservaría {env} existente")
        return

    if not env.exists() and ejemplo.exists():
        shutil.copy2(ejemplo, env)
        print(f"    {env} creado desde .env.example")
    if env.exists():
        texto = env.read_text(encoding="utf-8")
        if re.search(r"^OPENCODE_SERVER_PASSWORD=\s*$", texto, flags=re.M):
            texto = re.sub(
                r"^OPENCODE_SERVER_PASSWORD=\s*$",
                f"OPENCODE_SERVER_PASSWORD={secrets.token_urlsafe(24)}",
                texto,
                count=1,
                flags=re.M,
            )
            env.write_text(texto, encoding="utf-8")
            print("    contraseña del servidor OpenCode generada")

    precios, precios_ej = APP / "precios.json", APP / "precios.example.json"
    if not precios.exists() and precios_ej.exists():
        shutil.copy2(precios_ej, precios)
        print(f"    {precios.name} creado desde precios.example.json")

    for carpeta in ("logs", "tareas"):
        (plataforma.DATOS_DIR / carpeta).mkdir(parents=True, exist_ok=True)


def _escribir_json(path: Path, data: dict, dry_run: bool = False) -> None:
    if dry_run:
        print(f"    [dry-run] actualizaría JSON: {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent))
    with os.fdopen(fd, "w", encoding="utf-8") as fh:  # UTF-8 sin BOM (Claude Desktop no acepta BOM)
        json.dump(data, fh, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def _leer_json(path: Path, backup: bool = True, dry_run: bool = False) -> dict:
    if not path.exists():
        return {}
    if backup and not dry_run:
        shutil.copy2(path, path.with_name(path.name + ".bak"))
    try:
        return json.loads(path.read_text(encoding="utf-8-sig") or "{}")
    except ValueError:
        return {}


def config_claude_desktop() -> Path:
    return plataforma.config_claude_desktop()


def config_claude_code() -> Path:
    return plataforma.config_claude_code()


def merge_mcp_config(actual: dict, cmd: str, server_py: str, para_code: bool = False) -> dict:
    """Inserta o actualiza la entrada 'orchestrator' en mcpServers de forma idempotente sin borrar otros servidores."""
    res = dict(actual) if isinstance(actual, dict) else {}
    servers = dict(res.get("mcpServers") or {})
    entry: dict = {"command": cmd, "args": [server_py]}
    if para_code:
        entry = {"type": "stdio", "command": cmd, "args": [server_py], "env": servers.get("orchestrator", {}).get("env", {})}
    servers["orchestrator"] = entry
    res["mcpServers"] = servers
    return res


def renderizar_plantilla_claude_md(plantilla: str, env_vars: dict[str, str] | None = None) -> str:
    """Rellena los placeholders ({{TRABAJO_DIR}}, etc.) y reemplaza rutas D:\\ fijas si las hubiera según el SO."""
    ev = env_vars or {}
    trabajo = ev.get("TRABAJO_DIR") or os.getenv("TRABAJO_DIR") or str(plataforma.default_dir("workspaces"))
    personal = ev.get("PERSONAL_DIR") or os.getenv("PERSONAL_DIR") or str(plataforma.default_dir("personal"))
    universidad = ev.get("UNIVERSIDAD_DIR") or os.getenv("UNIVERSIDAD_DIR") or str(plataforma.default_dir("universidad"))
    repo = str(plataforma.REPO_ROOT)

    reemplazos = {
        "{{TRABAJO_DIR}}": trabajo,
        "{{PERSONAL_DIR}}": personal,
        "{{UNIVERSIDAD_DIR}}": universidad,
        "{{REPO_ROOT}}": repo,
        r"D:\workspaces": trabajo,
        r"D:\personal": personal,
        r"D:\universidad": universidad,
        r"D:\orquestador-claude": repo,
    }
    out = plantilla
    for k, v in reemplazos.items():
        out = out.replace(k, v)
    return out.strip()


def merge_claude_md(contenido_existente: str, bloque_nuevo: str) -> str:
    """Inserta o actualiza el bloque del orquestador en CLAUDE.md usando marcadores de inicio y fin,
    conservando intacto cualquier contenido del usuario fuera de los marcadores."""
    bloque_marcado = f"{MARCADOR_INICIO}\n{bloque_nuevo.strip()}\n{MARCADOR_FIN}"
    if not contenido_existente or not contenido_existente.strip():
        return bloque_marcado + "\n"

    patron = re.compile(
        re.escape(MARCADOR_INICIO) + r".*?" + re.escape(MARCADOR_FIN),
        flags=re.DOTALL,
    )
    if patron.search(contenido_existente):
        nuevo = patron.sub(lambda _: bloque_marcado, contenido_existente, count=1)
        return nuevo if nuevo.endswith("\n") else nuevo + "\n"

    # Si ya tenía la plantilla antigua sin marcadores (encabezado exacto y sin otro contenido), envolverla
    cabecera = "# Orquestador MCP: Claude es el cerebro"
    if contenido_existente.strip().startswith(cabecera) and len(contenido_existente.strip()) <= len(bloque_nuevo) + 200:
        return bloque_marcado + "\n"

    sep = "\n\n" if not contenido_existente.endswith("\n\n") else ""
    if contenido_existente.endswith("\n") and sep == "\n\n":
        sep = "\n"
    return contenido_existente + sep + bloque_marcado + "\n"


def instalar_claude_md(dry_run: bool = False) -> Path:
    """Instala o actualiza el bloque de reglas en ~/.claude/CLAUDE.md de manera idempotente."""
    plantilla_path = plataforma.REPO_ROOT / "config" / "CLAUDE.example.md"
    destino = plataforma.claude_dir() / "CLAUDE.md"
    if not plantilla_path.exists():
        return destino
    bloque = renderizar_plantilla_claude_md(plantilla_path.read_text(encoding="utf-8"))
    existente = destino.read_text(encoding="utf-8") if destino.exists() else ""
    nuevo = merge_claude_md(existente, bloque)
    if dry_run:
        print(f"    [dry-run] actualizaría bloque en {destino}")
        return destino
    destino.parent.mkdir(parents=True, exist_ok=True)
    if destino.exists() and existente != nuevo:
        shutil.copy2(destino, destino.with_name("CLAUDE.md.bak"))
    destino.write_text(nuevo, encoding="utf-8")
    print(f"    Reglas Claude Code (CLAUDE.md): {destino}")
    return destino


def merge_hooks_settings(actual: dict) -> dict:
    """Agrega el hook PostToolUse de cerrar_pedido en ~/.claude/settings.json de forma idempotente."""
    res = dict(actual) if isinstance(actual, dict) else {}
    hooks = dict(res.get("hooks") or {})
    post = list(hooks.get("PostToolUse") or [])
    if not any(isinstance(h, dict) and h.get("matcher") == "mcp__orchestrator__cerrar_pedido" for h in post):
        post.append(HOOK_CERRAR_PEDIDO)
    hooks["PostToolUse"] = post
    res["hooks"] = hooks
    return res


def instalar_hooks(dry_run: bool = False) -> Path:
    """Instala los hooks opcionales en ~/.claude/settings.json sin pisar configuraciones previas."""
    settings_path = plataforma.claude_dir() / "settings.json"
    actual = _leer_json(settings_path, backup=True, dry_run=dry_run)
    nuevo = merge_hooks_settings(actual)
    if actual != nuevo:
        _escribir_json(settings_path, nuevo, dry_run=dry_run)
    if not dry_run:
        print(f"    Hooks Claude Code (settings.json): {settings_path}")
    return settings_path


def registrar(py_exec: Path | None = None, dry_run: bool = False) -> None:
    py_cmd = str(py_exec or PY)
    srv_cmd = str(SERVER)

    desktop = config_claude_desktop()
    d = _leer_json(desktop, backup=True, dry_run=dry_run)
    d_nuevo = merge_mcp_config(d, py_cmd, srv_cmd, para_code=False)
    _escribir_json(desktop, d_nuevo, dry_run=dry_run)
    if not dry_run:
        print(f"    Claude Desktop: {desktop}")

    code = config_claude_code()
    c = _leer_json(code, backup=True, dry_run=dry_run)
    c_nuevo = merge_mcp_config(c, py_cmd, srv_cmd, para_code=True)
    _escribir_json(code, c_nuevo, dry_run=dry_run)
    if not dry_run:
        print(f"    Claude Code (todas las sesiones): {code}")

    instalar_claude_md(dry_run=dry_run)
    instalar_hooks(dry_run=dry_run)


def _leer_env_dict(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip()
    return out


def diagnosticar() -> dict:
    """Revisa el estado de la instalación y devuelve un diccionario con los chequeos e imprime un resumen claro."""
    env_file = APP / ".env"
    env_vals = _leer_env_dict(env_file)
    venv_py = plataforma.venv_python(APP / ".venv")

    oc_pref = env_vals.get("OPENCODE_CLI_PATH") or os.getenv("OPENCODE_CLI_PATH") or "opencode"
    agy_pref = env_vals.get("ANTIGRAVITY_CLI_PATH") or os.getenv("ANTIGRAVITY_CLI_PATH") or ""
    oc_bin = plataforma.buscar_opencode(oc_pref)
    agy_bin = plataforma.buscar_agy(agy_pref)
    oc_cfg = plataforma.default_opencode_config()
    agy_settings = Path.home() / ".gemini" / "antigravity-cli" / "settings.json"

    desktop_cfg = config_claude_desktop()
    code_cfg = config_claude_code()
    claude_md = plataforma.claude_dir() / "CLAUDE.md"

    desktop_ok = False
    if desktop_cfg.exists():
        try:
            desktop_ok = "orchestrator" in (json.loads(desktop_cfg.read_text(encoding="utf-8-sig")).get("mcpServers") or {})
        except (OSError, ValueError):
            pass

    code_ok = False
    if code_cfg.exists():
        try:
            code_ok = "orchestrator" in (json.loads(code_cfg.read_text(encoding="utf-8-sig")).get("mcpServers") or {})
        except (OSError, ValueError):
            pass

    claude_md_ok = claude_md.exists() and (
        MARCADOR_INICIO in claude_md.read_text(encoding="utf-8", errors="replace")
        or "Orquestador MCP" in claude_md.read_text(encoding="utf-8", errors="replace")
    )

    innti_prov = env_vals.get("INNTI_PROVIDER") or os.getenv("INNTI_PROVIDER") or "litellm-proxy"
    oc_cfg_ok = oc_cfg.is_file()

    estado = {
        "so": "Windows" if plataforma.es_windows() else ("macOS" if plataforma.es_macos() else sys.platform),
        "venv_ok": venv_py.is_file(),
        "venv_python": str(venv_py),
        "env_ok": env_file.is_file() and bool(env_vals.get("OPENCODE_SERVER_PASSWORD")),
        "env_path": str(env_file),
        "desktop_ok": desktop_ok,
        "desktop_path": str(desktop_cfg),
        "code_ok": code_ok,
        "code_path": str(code_cfg),
        "claude_md_ok": claude_md_ok,
        "claude_md_path": str(claude_md),
        "opencode_bin": oc_bin,
        "opencode_config_ok": oc_cfg_ok,
        "opencode_config_path": str(oc_cfg),
        "agy_bin": agy_bin,
        "agy_settings_ok": agy_settings.is_file(),
        "innti_provider": innti_prov,
    }

    print("\n==> Diagnóstico del Orquestador MCP")
    print(f"  - Sistema operativo:     {estado['so']}")
    print(f"  - Entorno virtual (.venv): {'[OK]' if estado['venv_ok'] else '[FALTA]'} ({estado['venv_python']})")
    print(f"  - Archivo .env:            {'[OK]' if estado['env_ok'] else '[FALTA]'} ({estado['env_path']})")
    print(f"  - MCP en Claude Code:      {'[OK] Registrado' if estado['code_ok'] else '[FALTA] No registrado'} ({estado['code_path']})")
    print(f"  - MCP en Claude Desktop:   {'[OK] Registrado' if estado['desktop_ok'] else '[FALTA] No registrado'} ({estado['desktop_path']})")
    print(f"  - Reglas en CLAUDE.md:     {'[OK] Instaladas' if estado['claude_md_ok'] else '[FALTA] Faltan'} ({estado['claude_md_path']})")
    print(f"  - OpenCode CLI:            {'[OK] ' + oc_bin if oc_bin else '[AVISO] No encontrado (instala: npm i -g opencode-ai o brew install opencode)'}")
    print(f"  - Config OpenCode / Innti: {'[OK] ' + str(oc_cfg) if oc_cfg_ok else '[AVISO] Falta ' + str(oc_cfg) + ' (define allí el proveedor ' + innti_prov + ', baseURL y apiKey)'}")
    print(f"  - Antigravity CLI (agy):   {'[OK] ' + agy_bin if agy_bin else '[AVISO] No encontrado (instala Antigravity e inicia sesión para crear ~/.gemini/bin/agy)'}")

    faltantes: list[str] = []
    if not oc_bin:
        faltantes.append(
            "Instalar OpenCode CLI (`npm i -g opencode-ai` o en macOS `brew install opencode`) "
            "o indicar su ruta en `OPENCODE_CLI_PATH` dentro de `mcp_orchestrator/.env`."
        )
    if not oc_cfg_ok:
        faltantes.append(
            f"Configurar el proveedor `{innti_prov}` (URL del proxy LiteLLM/Innti y clave) en `{oc_cfg}` "
            "o mediante variables de entorno en tu perfil de shell (`~/.zshrc` / variables de usuario) o en `mcp_orchestrator/.env` "
            "(ignorado por git). NUNCA pegues claves ni secretos en el chat con Claude."
        )
    if not agy_bin:
        faltantes.append(
            f"Instalar Antigravity y hacer login una vez (`{plataforma.default_agy()}`), o fijar `ANTIGRAVITY_CLI_PATH` en `mcp_orchestrator/.env`."
        )

    if faltantes:
        print("\n==> Acción manual requerida del usuario (no delegable sin tus credenciales/sesión):")
        for i, item in enumerate(faltantes, 1):
            print(f"  {i}. {item}")
    else:
        print("\n==> Todo listo. Reinicia Claude Code / Claude Desktop para cargar el servidor MCP.")

    return estado


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
    ap = argparse.ArgumentParser(description="Instalador multiplataforma del Orquestador MCP.")
    ap.add_argument("--solo-diagnostico", action="store_true", help="Solo verifica e imprime el diagnóstico sin modificar nada.")
    ap.add_argument("--dry-run", action="store_true", help="Muestra qué haría sin escribir archivos ni instalar paquetes.")
    ap.add_argument("--sin-registro", action="store_true", help="No registra en Claude Desktop / Claude Code.")
    ap.add_argument("--sin-deps", action="store_true", help="No crea ni actualiza el entorno virtual (.venv).")
    args = ap.parse_args(argv)

    if args.solo_diagnostico:
        diagnosticar()
        return 0

    py_target = PY
    if not args.sin_deps:
        print("==> Entorno virtual y dependencias")
        py_target = asegurar_venv(dry_run=args.dry_run)
    else:
        venv_cand = plataforma.venv_python(APP / ".venv")
        if venv_cand.is_file():
            py_target = venv_cand

    print("==> Configuración (.env)")
    preparar_env(dry_run=args.dry_run)

    if not args.sin_registro:
        print("==> Registro del servidor MCP y reglas")
        registrar(py_exec=py_target, dry_run=args.dry_run)

    diagnosticar()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
