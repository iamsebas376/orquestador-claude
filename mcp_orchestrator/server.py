"""
MCP Orchestrator: Claude actúa como cerebro/enrutador y delega la ejecución pesada.

  TRABAJO  -> OpenCode CLI usando modelos Innti (proveedor LiteLLM corporativo)
  PERSONAL -> Antigravity (CLI oficial `agy`)

Las salidas largas se escriben en disco; a Claude solo se le devuelve un resumen corto.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

logging.getLogger("httpx").setLevel(logging.WARNING)

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

import plataforma  # noqa: E402
import registro  # noqa: E402  (después de cargar .env: lee TAREAS_DIR, MAX_RONDAS_CORRECCION, etc.)
import control  # noqa: E402


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


# Por defecto ~/workspaces, ~/personal, ~/universidad (cámbialos en .env, p. ej. D:\workspaces en Windows).
TRABAJO_DIR = Path(_env("TRABAJO_DIR") or plataforma.default_dir("workspaces")).expanduser()
PERSONAL_DIR = Path(_env("PERSONAL_DIR") or plataforma.default_dir("personal")).expanduser()
UNIVERSIDAD_DIR = Path(_env("UNIVERSIDAD_DIR") or plataforma.default_dir("universidad")).expanduser()
# Todo lo que no es trabajo va a Antigravity: además de personal y universidad, otras carpetas propias
# (por defecto la del propio orquestador). Separadas por coma en OTRAS_CARPETAS_PERSONALES.
OTRAS_CARPETAS = [Path(c.strip()).expanduser() for c in
                  _env("OTRAS_CARPETAS_PERSONALES", str(plataforma.REPO_ROOT)).split(",") if c.strip()]
LOGS_DIR = Path(_env("LOGS_DIR") or plataforma.DATOS_DIR / "logs").expanduser()

OPENCODE_CLI_PATH = _env("OPENCODE_CLI_PATH", "opencode")
OPENCODE_TIMEOUT = int(_env("OPENCODE_TIMEOUT_SECONDS", "1800"))
# Segundos sin una sola línea de salida tras los que se corta la tarea (modelo colgado o sin red).
OPENCODE_IDLE = int(_env("OPENCODE_IDLE_SECONDS", "240"))
# Servidor OpenCode persistente: las tareas corren en él y sus sesiones permiten rondas de corrección (y ver la TUI en una pestaña).
OPENCODE_SERVER_URL = _env("OPENCODE_SERVER_URL", "http://127.0.0.1:4096").rstrip("/")
INNTI_PROVIDER = _env("INNTI_PROVIDER", "litellm-proxy")
INNTI_DEFAULT_MODEL = _env("INNTI_DEFAULT_MODEL", "innti-dsf-last")

ANTIGRAVITY_CLI_PATH = _env("ANTIGRAVITY_CLI_PATH") or plataforma.default_agy()
ANTIGRAVITY_AGY_MODEL = _env("ANTIGRAVITY_AGY_MODEL", "gemini-3.8-flash-medium")
# Orden de respaldo cuando un modelo llega a su límite: se pasa al siguiente de la lista (de más a menos capaz).
ANTIGRAVITY_CADENA = [
    m.strip() for m in _env(
        "ANTIGRAVITY_CADENA", "gemini-4-argon-high,gemini-4-argon-medium,gemini-3.8-flash-medium,gemini-3.8-flash-low"
    ).split(",") if m.strip()
]
ANTIGRAVITY_TIMEOUT = int(_env("ANTIGRAVITY_TIMEOUT_SECONDS", "900"))
# La TUI de Antigravity que corre en la pestaña de Claude aprueba todo sin preguntar (solo carpetas personales):
# 0 = que pregunte por cada comando.
ANTIGRAVITY_SIN_PERMISOS = _env("ANTIGRAVITY_SIN_PERMISOS", "1") in ("1", "true", "si", "sí")

MAX_RETURN_CHARS = int(_env("MAX_RETURN_CHARS", "1500"))
PANEL_URL = _env("PANEL_URL")
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "dist", "build", "target", ".next", "__pycache__", ".angular",
             ".pytest_cache", ".mypy_cache", ".ruff_cache", ".gradle", ".idea", ".vscode"}

mcp =FastMCP("orchestrator")


def _podar_logs(dias: int = 30) -> None:
    """Borra logs de ejecución con más de `dias` días: a nadie le sirven y se acumulan."""
    limite = time.time() - dias * 86400
    for sub in ("trabajo", "personal", "verificacion"):
        for f in (LOGS_DIR / sub).glob("*"):
            try:
                if f.is_file() and f.stat().st_mtime < limite:
                    f.unlink()
            except OSError:
                pass


# ----------------------------------------------------------------------------- utilidades

LIVE_LOG = LOGS_DIR / "en-vivo.log"


def _live(msg: str) -> None:
    """Registro de texto en tiempo real (logs/en-vivo.log): Get-Content -Wait."""
    try:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        with open(LIVE_LOG, "a", encoding="utf-8") as fh:
            fh.write(f"{datetime.now():%H:%M:%S} {msg}\n")
    except OSError:
        pass


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _slug(text: str, n: int = 40) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", text.lower()).strip("-")
    return (s[:n] or "tarea").strip("-")


def _tail(text: str, limit: int = MAX_RETURN_CHARS) -> str:
    text = text.strip()
    return text if len(text) <= limit else "…" + text[-limit:]


def _resolve_under(base: Path, ruta: str) -> Path:
    """Ruta absoluta se respeta tal cual; relativa se ubica dentro de `base`."""
    if not ruta:
        return base
    p = Path(ruta)
    return p if p.is_absolute() else base / p


def _inside(path: Path, *roots: Path) -> bool:
    p = path.resolve()
    return any(p == r.resolve() or r.resolve() in p.parents for r in roots)


def _resolve_personal(ruta: str) -> Path:
    """Relativa: 'universidad\\...' va a UNIVERSIDAD_DIR, cualquier otra a PERSONAL_DIR."""
    if ruta and not Path(ruta).is_absolute():
        first, _, rest = ruta.replace("/", "\\").partition("\\")
        if first.lower() == UNIVERSIDAD_DIR.name.lower():
            return UNIVERSIDAD_DIR / rest
    return _resolve_under(PERSONAL_DIR, ruta)


def _snapshot(root: Path, limit: int = 20000) -> dict[str, float]:
    """mtimes de archivos bajo root (ignorando carpetas pesadas) para detectar cambios."""
    if root.is_file():
        return {str(root): root.stat().st_mtime}
    snap: dict[str, float] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for f in filenames:
            fp = os.path.join(dirpath, f)
            try:
                snap[fp] = os.path.getmtime(fp)
            except OSError:
                pass
            if len(snap) >= limit:
                return snap
    return snap


def _count_lines(paths: list[str]) -> int:
    total = 0
    for p in paths:
        try:
            with open(p, "rb") as fh:
                total += sum(1 for _ in fh)
        except OSError:
            pass
    return total


def _opencode_cmd() -> str | None:
    if Path(OPENCODE_CLI_PATH).is_file():
        return OPENCODE_CLI_PATH
    return shutil.which(OPENCODE_CLI_PATH)


# Incluye OPENCODE_SERVER_PASSWORD (cargada del .env): serve exige la contraseña y run/attach la envían.
OPENCODE_ENV = {**os.environ, "OPENCODE_DISABLE_CLAUDE_CODE_PROMPT": "1"}

_OC_CLIENT: httpx.AsyncClient | None = None
_OC_CLIENT_KEY: tuple | None = None


def _oc_auth() -> tuple[str, str] | None:
    """Basic auth para la API del servidor OpenCode (usuario por defecto 'opencode')."""
    pwd = os.getenv("OPENCODE_SERVER_PASSWORD")
    return (os.getenv("OPENCODE_SERVER_USERNAME") or "opencode", pwd) if pwd else None


def _oc_client() -> httpx.AsyncClient:
    """Cliente HTTP persistente hacia `opencode serve` (reutiliza conexiones TCP en el loop activo)."""
    global _OC_CLIENT, _OC_CLIENT_KEY
    try:
        loop_id = id(asyncio.get_running_loop())
    except RuntimeError:
        loop_id = 0
    key = (OPENCODE_SERVER_URL, _oc_auth(), loop_id)
    if _OC_CLIENT is None or _OC_CLIENT.is_closed or _OC_CLIENT_KEY != key:
        _OC_CLIENT = httpx.AsyncClient(
            base_url=OPENCODE_SERVER_URL,
            auth=_oc_auth(),
            timeout=httpx.Timeout(30.0, connect=5.0),
        )
        _OC_CLIENT_KEY = key
    return _OC_CLIENT


async def _ensure_opencode_server(cli: str) -> bool:
    """Arranca `opencode serve` en segundo plano si no responde. Devuelve True si queda disponible."""
    c = _oc_client()
    for intento in range(30):
        try:
            if (await c.get("/project", timeout=2.0)).status_code < 500:
                return True
        except httpx.HTTPError:
            pass
        if intento == 0:
            port = OPENCODE_SERVER_URL.rsplit(":", 1)[-1]
            _live(f"  iniciando servidor OpenCode en 127.0.0.1:{port}…")
            subprocess.Popen(
                [cli, "serve", "--hostname", "127.0.0.1", "--port", port], cwd=str(TRABAJO_DIR), env=OPENCODE_ENV,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        await asyncio.sleep(0.5)
    return False


async def _create_session(cwd: Path, title: str) -> str | None:
    """Crea la sesión en el servidor de OpenCode. Puede tardar varios segundos con otras tareas en marcha: 2 intentos."""
    c = _oc_client()
    for intento in (1, 2):
        try:
            r = await c.post("/session", params={"directory": str(cwd)}, json={"title": title}, timeout=30.0)
            r.raise_for_status()
            return r.json()["id"]
        except (httpx.HTTPError, KeyError, ValueError) as e:
            _live(f"  ✖ no se pudo crear la sesión en el servidor (intento {intento}): {type(e).__name__} {e}")
    return None


async def _run(cmd: list[str], cwd: Path, timeout: int, idle: int = 0, tag: str = "") -> tuple[int, str]:
    """Ejecuta `cmd` mostrando cada línea en el seguimiento en vivo (con `tag` para distinguir tareas en paralelo)
    y un aviso cada 20 s si el agente sigue trabajando sin imprimir nada."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(cwd),
        stdin=asyncio.subprocess.DEVNULL,
        # OpenCode lee ~/.claude/CLAUDE.md por compatibilidad; esas reglas son para Claude, no para él.
        env=OPENCODE_ENV,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    lines: list[str] = []

    inactivo = False
    inicio_t = ultimo_aviso = time.monotonic()
    tick = min(20, idle) if idle else 20

    async def pump() -> None:
        nonlocal inactivo, ultimo_aviso
        assert proc.stdout
        silencio = 0.0
        while True:
            try:
                raw = await asyncio.wait_for(proc.stdout.readline(), timeout=tick)
            except asyncio.TimeoutError:
                silencio += tick
                if idle and silencio >= idle:
                    inactivo = True
                    return
                ahora = time.monotonic()
                if ahora - ultimo_aviso >= 20:
                    _live(f"  … {tag}sigue trabajando ({int(ahora - inicio_t)} s)")
                    ultimo_aviso = ahora
                continue
            silencio = 0.0
            if not raw:
                return
            line = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", raw.decode("utf-8", errors="replace")).rstrip()
            lines.append(line)
            if line:
                _live(f"  │ {tag}{line}")
                ultimo_aviso = time.monotonic()

    try:
        await asyncio.wait_for(pump(), timeout=timeout)
        if inactivo:
            proc.kill()
            await proc.wait()
            _live(f"  ✖ sin salida durante {idle}s: se cortó")
            return -1, "\n".join(lines) + f"\nSin salida durante {idle}s: se cortó la tarea."
        await proc.wait()
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        _live(f"  ✖ timeout tras {timeout}s")
        return -1, f"Timeout tras {timeout}s"
    return proc.returncode or 0, "\n".join(lines)


# ----------------------------------------------------------------------------- herramientas

def _git_checkpoint(cwd: Path) -> str:
    """Commit actual y cambios previos sin commit, para poder deshacer lo que haga el subagente."""
    try:
        head = subprocess.run(["git", "-C", str(cwd), "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL)
        if head.returncode != 0:
            return ""
        status = subprocess.run(["git", "-C", str(cwd), "status", "--porcelain"],
                                capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL)
        previos = len([l for l in status.stdout.splitlines() if l.strip()])
        return (f"commit {head.stdout.strip()}" + (f" (+{previos} cambios previos sin commit)" if previos else "") +
                f". Para deshacer solo lo del agente: git -C \"{cwd}\" diff y revierte esos archivos.")
    except (OSError, subprocess.SubprocessError):
        return ""


def _git_cambios(cwd: Path) -> tuple[list[str], int]:
    """Archivos modificados/nuevos y líneas tocadas según git (`status --porcelain` y `diff --numstat`)."""
    try:
        st = subprocess.run(["git", "-C", str(cwd), "status", "--porcelain", "-uall"],
                            capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL)
        if st.returncode != 0:
            return [], 0
        archivos: list[str] = []
        for line in st.stdout.splitlines():
            if len(line) > 3:
                rel = line[3:].strip().strip('"')
                if " -> " in rel:
                    rel = rel.split(" -> ", 1)[1].strip('"')
                archivos.append(str((cwd / rel).resolve()))
        changed = sorted(set(archivos))
        num = subprocess.run(["git", "-C", str(cwd), "diff", "--numstat", "HEAD"],
                             capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL)
        lineas = 0
        if num.returncode == 0:
            for line in num.stdout.splitlines():
                parts = line.split("\t")
                if len(parts) >= 2:
                    lineas += (int(parts[0]) if parts[0].isdigit() else 0) + (int(parts[1]) if parts[1].isdigit() else 0)
        if not lineas and changed:
            lineas = _count_lines(changed)
        return changed, lineas
    except (OSError, subprocess.SubprocessError):
        return [], 0


async def _session_tokens(session_id: str, cwd: Path, desde_ms: int) -> dict:
    """Suma los tokens de los mensajes del asistente creados en esta ejecución de la sesión de OpenCode."""
    tokens, _, _, _, _, _ = await _session_estado(session_id, cwd, desde_ms)
    return tokens


async def _session_estado(session_id: str, cwd: Path, desde_ms: int, incluir_diff: bool = False) -> tuple[dict, str, list[str], int, bool, str]:
    """Lee mensajes (y opcionalmente `/diff`) de la sesión en `opencode serve`:
    devuelve (tokens, texto_salida, archivos_diff, lineas_diff, terminado, error)."""
    tot = {"input": 0, "output": 0, "reasoning": 0, "cache_read": 0}
    textos: list[str] = []
    diffs_map: dict[str, int] = {}
    terminado = False
    error = ""
    c = _oc_client()
    try:
        r = await c.get(f"/session/{session_id}/message", params={"directory": str(cwd)}, timeout=10.0)
        r.raise_for_status()
        msgs = r.json()
        for msg in msgs:
            info = msg.get("info") or {}
            created = (info.get("time") or {}).get("created", 0)
            if created >= desde_ms - 2000:
                for d in ((info.get("summary") or {}).get("diffs") or []):
                    if f := d.get("file"):
                        p = str((cwd / f).resolve())
                        diffs_map[p] = diffs_map.get(p, 0) + int(d.get("additions") or 0) + int(d.get("deletions") or 0)
            if info.get("role") != "assistant" or created < desde_ms - 1000:
                continue
            t = info.get("tokens") or {}
            tot["input"] += t.get("input", 0)
            tot["output"] += t.get("output", 0)
            tot["reasoning"] += t.get("reasoning", 0)
            tot["cache_read"] += (t.get("cache") or {}).get("read", 0)
            if err := info.get("error"):
                error = str(err.get("data", {}).get("message") or err.get("name") or err) if isinstance(err, dict) else str(err)
            for p in msg.get("parts") or []:
                if p.get("type") == "text" and p.get("text"):
                    textos.append(str(p["text"]).strip())
        if msgs:
            ult = (msgs[-1].get("info") or {})
            if ult.get("role") == "assistant" and (ult.get("time") or {}).get("created", 0) >= desde_ms - 1000:
                if ult.get("finish") == "stop" and (ult.get("time") or {}).get("completed"):
                    terminado = True
    except (httpx.HTTPError, ValueError):
        pass
    if incluir_diff:
        try:
            rd = await c.get(f"/session/{session_id}/diff", params={"directory": str(cwd)}, timeout=10.0)
            if rd.status_code == 200:
                for d in rd.json() or []:
                    if isinstance(d, dict) and (f := d.get("file")):
                        p = str((cwd / f).resolve())
                        diffs_map.setdefault(p, int(d.get("additions") or 0) + int(d.get("deletions") or 0))
        except (httpx.HTTPError, ValueError):
            pass
    archivos = sorted(diffs_map)
    lineas = sum(diffs_map.values()) or (_count_lines(archivos) if archivos else 0)
    return tot, "\n\n".join(t for t in textos if t), archivos, lineas, terminado, error


async def _run_opencode_http(
    session_id: str, cwd: Path, modelo: str, prompt: str, timeout: int, desde_ms: int, tag: str = ""
) -> tuple[int, str, dict, list[str], int]:
    """Envía el prompt por HTTP (`prompt_async`) a `opencode serve` y detecta el fin por SSE (`GET /event`),
    con reconexión automática y verificación de respaldo sobre `/session/{id}/message`."""
    prov, _, mod = modelo.partition("/")
    body = {"model": {"providerID": prov, "modelID": mod}, "parts": [{"type": "text", "text": prompt}]}
    fin = asyncio.Event()
    err_sse: list[str] = []
    vistos: set[str] = set()
    inicio_t = ultimo_aviso = time.monotonic()

    async def escuchar_sse() -> None:
        nonlocal ultimo_aviso
        while not fin.is_set():
            try:
                async with httpx.AsyncClient(base_url=OPENCODE_SERVER_URL, auth=_oc_auth(), timeout=None) as sse_client:
                    async with sse_client.stream("GET", "/event", params={"directory": str(cwd)}) as resp:
                        async for line in resp.aiter_lines():
                            if fin.is_set():
                                return
                            if not line.startswith("data:"):
                                continue
                            try:
                                ev = json.loads(line[5:].strip())
                            except ValueError:
                                continue
                            tipo = ev.get("type", "")
                            props = ev.get("properties") or {}
                            if tipo == "message.part.updated":
                                part = props.get("part") or {}
                                if part.get("sessionID") == session_id and part.get("type") == "tool":
                                    estado_t = (part.get("state") or {}).get("status", "")
                                    clave = f"{part.get('id')}:{estado_t}"
                                    if estado_t in ("running", "completed") and clave not in vistos:
                                        vistos.add(clave)
                                        _live(f"  │ {tag}tool {part.get('tool')} ({estado_t})")
                                        ultimo_aviso = time.monotonic()
                            elif tipo == "session.idle" and props.get("sessionID") == session_id:
                                fin.set()
                                return
                            elif tipo == "session.status" and props.get("sessionID") == session_id:
                                if (props.get("status") or {}).get("type") == "idle":
                                    fin.set()
                                    return
                            elif tipo == "session.error" and props.get("sessionID") in (session_id, None, ""):
                                err = props.get("error") or props
                                err_sse.append(str(err.get("data", {}).get("message") or err.get("name") or err)
                                               if isinstance(err, dict) else str(err))
                                fin.set()
                                return
            except (httpx.HTTPError, asyncio.CancelledError):
                if fin.is_set():
                    return
                await asyncio.sleep(1.0)

    sse_task = asyncio.create_task(escuchar_sse())
    try:
        c = _oc_client()
        r = await c.post(f"/session/{session_id}/prompt_async", params={"directory": str(cwd)}, json=body, timeout=30.0)
        if r.status_code >= 400:
            r.raise_for_status()
        limite = inicio_t + timeout
        while time.monotonic() < limite and not fin.is_set():
            try:
                await asyncio.wait_for(fin.wait(), timeout=2.5)
                break
            except asyncio.TimeoutError:
                _, _, _, _, terminado, error_msg = await _session_estado(session_id, cwd, desde_ms, incluir_diff=False)
                if error_msg:
                    err_sse.append(error_msg)
                    fin.set()
                    break
                if terminado:
                    fin.set()
                    break
                ahora = time.monotonic()
                if ahora - ultimo_aviso >= 20:
                    _live(f"  … {tag}sigue trabajando ({int(ahora - inicio_t)} s)")
                    ultimo_aviso = ahora
        if not fin.is_set():
            _live(f"  ✖ timeout tras {timeout}s")
            tokens, out, archivos, lineas, _, _ = await _session_estado(session_id, cwd, desde_ms, incluir_diff=True)
            return -1, (out + f"\nTimeout tras {timeout}s").strip(), tokens, archivos, lineas
    except httpx.HTTPError as e:
        return -1, f"Error HTTP comunicando con OpenCode: {type(e).__name__} {e}", {}, [], 0
    finally:
        fin.set()
        sse_task.cancel()

    tokens, out, archivos, lineas, _, error_msg = await _session_estado(session_id, cwd, desde_ms, incluir_diff=True)
    if err_sse or error_msg:
        detalle = err_sse[0] if err_sse else error_msg
        _live(f"  ✖ error en sesión OpenCode: {detalle[:200]}")
        return 1, (f"{out}\nError: {detalle}".strip() if out else f"Error: {detalle}"), tokens, archivos, lineas
    return 0, out, tokens, archivos, lineas


def _work_cwd(target: Path) -> Path:
    """Carpeta de trabajo de OpenCode para una ruta (archivo → su carpeta), creándola si no existe."""
    if target.suffix and not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        return target.parent
    if target.is_file():
        return target.parent
    target.mkdir(parents=True, exist_ok=True)
    return target


@mcp.tool()
async def preparar_vista_opencode(ruta_proyecto_o_archivo: str = "", titulo: str = "") -> str:
    """TRABAJO — Paso 1 de 3 (obligatorio antes de delegar): crea la sesión en `opencode serve` y devuelve
    `session_id`, `ruta_para_ejecutar` y el comando TUI (`oc_attach.py`).
    Paso 2: ejecuta el comando con `run_in_terminal` (título 'OpenCode') y muestra la pestaña con `show_pane`.
    Paso 3: llama `ejecutar_trabajo_opencode_innti` pasando `sesion_opencode=<session_id>`."""
    target = _resolve_under(TRABAJO_DIR, ruta_proyecto_o_archivo)
    if not _inside(target, TRABAJO_DIR):
        return (f"Rechazado: {target} está fuera de {TRABAJO_DIR}. Si es un proyecto personal o de "
                "universidad usa preparar_tarea_antigravity.")
    cli = _opencode_cmd()
    if not cli:
        return f"Error: no se encontró OpenCode ('{OPENCODE_CLI_PATH}'). Revisa OPENCODE_CLI_PATH en .env."
    if not await _ensure_opencode_server(cli):
        return f"Error: el servidor de OpenCode no respondió en {OPENCODE_SERVER_URL}."
    cwd = _work_cwd(target)
    session_id = await _create_session(cwd, f"Claude → {titulo or cwd.name}"[:80])
    if not session_id:
        return "Error: no se pudo crear la sesión en el servidor de OpenCode."
    # oc_attach.py lee la contraseña del .env: no aparece en el comando ni en el chat.
    cmd = plataforma.run_line(sys.executable, plataforma.quote_path(BASE_DIR / "oc_attach.py"),
                              "--dir", plataforma.quote_path(cwd), "--session", session_id)
    return (f"session_id: {session_id}\nruta_para_ejecutar: {target}\n"
            f"comando (ejecútalo con run_in_terminal, una sola línea, sin cambios; luego muestra la pestaña con show_pane):\n{cmd}")


@mcp.tool()
async def ejecutar_trabajo_opencode_innti(
    prompt_tarea: str,
    modelo_innti: str = "",
    ruta_proyecto_o_archivo: str = "",
    sesion_opencode: str = "",
    pedido_id: str = "",
    item: int = 0,
    forzar_presupuesto: bool = False,
    sin_terminal: bool = False,
) -> str:
    """TRABAJO — Paso 3 de 3 (dentro de TRABAJO_DIR): envía la tarea por HTTP a la `sesion_opencode` abierta en la
    pestaña de terminal (`preparar_vista_opencode` → `run_in_terminal` + `show_pane` → `ejecutar_trabajo_opencode_innti`).
    Rechaza llamadas sin `sesion_opencode` salvo que `sin_terminal=True` (solo en Chat sin terminal).
    En rondas de corrección del mismo ítem, reutiliza el mismo `sesion_opencode`.
    Modelos (`modelo_innti`): vacío = innti-dsf-last (rápido/barato); innti-dsp lógica compleja/revisión;
    innti-dev docs/commits; innti-gpt planes/imágenes; innti-terra arquitectura. Indica rutas exactas en `prompt_tarea`."""
    target = _resolve_under(TRABAJO_DIR, ruta_proyecto_o_archivo)
    _live("")
    _live(f"▶ TRABAJO → OpenCode | ruta: {target}")
    _live(f"  prompt: {prompt_tarea[:300]}")
    if not _inside(target, TRABAJO_DIR):
        _live(f"  ✖ rechazado: ruta fuera de {TRABAJO_DIR}")
        return (f"Rechazado: {target} está fuera de {TRABAJO_DIR}. Si es un proyecto personal o de "
                "universidad usa preparar_tarea_antigravity.")

    cli = _opencode_cmd()
    if not cli:
        return (f"Error: no se encontró OpenCode ('{OPENCODE_CLI_PATH}'). "
                "Instálalo (npm i -g opencode-ai) o fija OPENCODE_CLI_PATH en .env.")

    modelo = (modelo_innti or INNTI_DEFAULT_MODEL).strip()
    if "/" not in modelo:
        modelo = f"{INNTI_PROVIDER}/{modelo}"

    bloqueado, presupuesto = control.estado_presupuesto()
    if bloqueado and not forzar_presupuesto:
        _live(f"  ⛔ freno de presupuesto: {presupuesto}")
        return (f"Freno de presupuesto: {presupuesto}\nNo se delegó nada. Pide autorización al usuario y, si la da, "
                "repite con forzar_presupuesto=True (o ajusta PRESUPUESTO_DIA_USD / PRESUPUESTO_MES_USD en .env).")

    cwd = _work_cwd(target)
    if not sesion_opencode and not sin_terminal:
        _live("  ✖ rechazado: falta sesion_opencode (no se preparó pestaña de terminal)")
        return (
            "Error: falta `sesion_opencode` (no se permite delegar a OpenCode en modo interno sin pestaña). "
            "Ejecuta en orden:\n"
            f"1. `preparar_vista_opencode(ruta_proyecto_o_archivo=\"{ruta_proyecto_o_archivo or cwd}\")`\n"
            "2. `run_in_terminal` con el comando devuelto (título 'OpenCode') y `show_pane`\n"
            "3. `ejecutar_trabajo_opencode_innti(..., sesion_opencode=<session_id>)`\n"
            "(Solo en Claude Chat, donde no existe `run_in_terminal`, pasa explícitamente `sin_terminal=True`)."
        )

    aviso_item = ""
    if not (pedido_id and item):  # ajuste sin pedido: queda como ítem nuevo del pedido del proyecto
        pedido_id, item, aviso_item = registro.item_automatico(str(target if not target.suffix else target.parent),
                                                               prompt_tarea)
    if pedido_id and item:
        registro.actualizar_item(pedido_id, item, "en_curso", agente="OpenCode", modelo=modelo.split("/")[-1])

    prompt = prompt_tarea
    if target.suffix:
        prompt = f"Archivo objetivo: {target}\n\n{prompt_tarea}"
    prompt += ("\n\nNo instales paquetes globales ni cambies la configuración del sistema; si falta una "
               "dependencia, usa la del proyecto o indícalo en el resumen."
               "\nSé rápido: trabaja solo con las rutas que te indico, sin explorar el repo para ubicar archivos "
               "(nunca node_modules, .nx ni dist); escribe los archivos grandes por partes; no lances comandos que pasen de 2 minutos "
               "(servidores, builds completos, instalaciones); si hace falta uno, dilo en el resumen."
               "\nEscribe los cambios directamente en disco. Al final responde SOLO con un "
               "resumen breve (máx. 10 líneas) de lo realizado y los archivos tocados.")

    titulo = f"Claude → {prompt_tarea[:60]}"
    session_id = sesion_opencode.strip()
    servidor = await _ensure_opencode_server(cli)
    if servidor and not session_id:
        session_id = await _create_session(cwd, titulo) or ""

    checkpoint = await asyncio.to_thread(_git_checkpoint, cwd)
    before = {} if checkpoint else await asyncio.to_thread(_snapshot, cwd)
    inicio_ms = int(time.time() * 1000)
    etiqueta = f"[OpenCode{f' #{item}' if item else ''} {modelo.split('/')[-1]}] "

    if servidor and session_id:
        _live(f"  modelo: {modelo} | sesión HTTP: {session_id} en {cwd}")
        code, output, tokens, changed, lineas = await _run_opencode_http(
            session_id, cwd, modelo, prompt, OPENCODE_TIMEOUT, inicio_ms, tag=etiqueta
        )
    else:
        _live(f"  modelo: {modelo} | fallback CLI opencode run en {cwd}")
        cmd = [cli, "run", "--model", modelo, "--dir", str(cwd)]
        code, output = await _run([*cmd, prompt], cwd, OPENCODE_TIMEOUT, idle=OPENCODE_IDLE, tag=etiqueta)
        tokens = {}
        changed, lineas = [], 0

    if not changed:
        if checkpoint:
            changed, lineas = await asyncio.to_thread(_git_cambios, cwd)
        else:
            after = await asyncio.to_thread(_snapshot, cwd)
            changed = sorted(p for p, m in after.items() if before.get(p) != m)
            lineas = await asyncio.to_thread(_count_lines, changed)
    costo = registro.costo_innti(modelo, tokens)
    registro.registrar_costo("opencode", modelo.split("/")[-1], tokens, costo, pedido_id, item, str(cwd),
                             sesion=session_id)

    log_dir = LOGS_DIR / "trabajo"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"{_stamp()}-{_slug(prompt_tarea)}.md"
    log_file.write_text(
        f"# OpenCode ({modelo})\n\n- cwd: {cwd}\n- exit: {code}\n\n## Prompt\n\n{prompt}\n\n"
        f"## Archivos modificados\n\n" + "\n".join(f"- {p}" for p in changed) +
        f"\n\n## Salida\n\n{output}\n",
        encoding="utf-8",
    )

    estado = "Éxito" if code == 0 else f"Error (exit {code})"
    shown = "\n".join(f"  - {p}" for p in changed[:15]) + (f"\n  … y {len(changed) - 15} más" if len(changed) > 15 else "")
    tok_txt = (f"{tokens.get('input', 0) + tokens.get('cache_read', 0):,} entrada · "
               f"{tokens.get('output', 0) + tokens.get('reasoning', 0):,} salida · ≈ US$ {costo:.4f}").replace(",", ".")
    _live(f"■ {estado} | {len(changed)} archivos | {tok_txt} | resumen devuelto a Claude")
    vinculo = registro.vincular_delegacion(pedido_id, item, "OpenCode", modelo.split("/")[-1], costo, estado)
    vinculo += aviso_item
    revisar = f"Revisa con: git -C \"{cwd}\" diff" if checkpoint else "Revisa los archivos listados."
    _, presupuesto = control.estado_presupuesto()
    alerta = f"\n{presupuesto}" if presupuesto.startswith(("⚠", "⛔")) else ""
    if len(prompt_tarea) > 3000:
        alerta += "\n⚠ Prompt muy largo: parte la tarea en varias delegaciones (ts/html, luego i18n/integración) para evitar el timeout del proxy."
    if not target.suffix and not re.search(r"[\w-]+\.\w{1,5}\b|[A-Za-z]:\\", prompt_tarea):
        alerta += "\n⚠ El prompt no traía rutas de archivo: el agente tuvo que explorar. La próxima vez da las rutas exactas."
    return (
        f"{estado}: OpenCode usando el modelo {modelo} de Innti en {target}.\n"
        f"Archivos modificados: {len(changed)} | Líneas procesadas: {lineas} | Tokens: {tok_txt}\n"
        f"{shown}\n"
        + (f"sesion_opencode: {session_id} (pásala en una ronda de corrección para conservar el contexto)\n"
           if session_id else "")
        + (f"Punto de retorno: {checkpoint}\n" if checkpoint else "")
        + f"{revisar}\nLog completo: {log_file}{vinculo}{alerta}\n\nResumen de OpenCode:\n{_tail(output)}"
    )


def _cierre_personal(res: str, pedido_id: str, item: int, model: str, folder: Path) -> str:
    """Registra el consumo y vincula el resultado al checklist. Plan Pro: sin gasto real; se registra también
    el costo equivalente por la API de Gemini (no cuenta para el presupuesto de Innti)."""
    m = re.search(r"Tokens: ([\d.]+) entrada(?: \(([\d.]+) de caché\))? / ([\d.]+) salida", res)
    tokens = {}
    if m:
        total_in = int(m.group(1).replace(".", ""))
        cache = int((m.group(2) or "0").replace(".", ""))
        tokens = {"input": total_in - cache, "cache_read": cache, "output": int(m.group(3).replace(".", ""))}
    equivalente = registro.costo_innti(model, tokens) if tokens else 0.0
    conv = re.search(r"conversacion_antigravity: (\S+)", res)
    registro.registrar_costo("antigravity", model, tokens, 0.0, pedido_id, item, str(folder), equivalente,
                             sesion=conv.group(1) if conv else "")
    if res.startswith("Límite") and pedido_id and item:
        sig = re.search(r"siguiente_modelo: (\S+)", res)
        registro.actualizar_item(pedido_id, item, "en_curso",
                                 nota=f"Límite de {model} en Antigravity → " + (
                                     f"se reintenta con {sig.group(1)}" if sig and sig.group(1) != "ninguno"
                                     else "sin modelos de respaldo"))
    if not res.startswith("Éxito"):
        return res
    extra = f"\nEquivalente por la API de Gemini: ≈ US$ {equivalente:.4f} (plan Pro: no se cobra)" if equivalente else ""
    return res + extra + registro.vincular_delegacion(pedido_id, item, "Antigravity", model, 0.0, res.split("\n")[0])


AGY_SETTINGS = Path.home() / ".gemini" / "antigravity-cli" / "settings.json"


def _agy_confiar(folder: Path) -> bool:
    """Marca la carpeta como confiable en agy para que no pida confirmarla. Solo carpetas personales o de
    universidad: la ruta ya pasó por _inside antes de llegar aquí, pero se vuelve a comprobar."""
    if not _inside(folder, PERSONAL_DIR, UNIVERSIDAD_DIR, *OTRAS_CARPETAS):
        return False
    try:
        data = json.loads(AGY_SETTINGS.read_text(encoding="utf-8")) if AGY_SETTINGS.exists() else {}
    except (OSError, ValueError):
        return False  # si el archivo está dañado no se toca: agy pedirá confirmar como siempre
    lista = data.setdefault("trustedWorkspaces", [])
    clave = os.path.normcase(os.path.normpath(str(folder)))
    if any(os.path.normcase(os.path.normpath(w)) == clave for w in lista):
        return True
    lista.append(str(folder))
    try:
        AGY_SETTINGS.parent.mkdir(parents=True, exist_ok=True)
        AGY_SETTINGS.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError:
        return False
    return True


def _agy_cmd() -> str | None:
    if Path(ANTIGRAVITY_CLI_PATH).is_file():
        return ANTIGRAVITY_CLI_PATH
    return shutil.which(ANTIGRAVITY_CLI_PATH)


def _agy_prompt(instruccion: str, contexto: str, target: Path, folder: Path, resultado: Path | None = None) -> str:
    destino = f" Guarda el resultado principal en {target}." if target.suffix else ""
    return (
        f"[Tarea delegada por Claude] {instruccion}"
        + (f" Contexto adicional: {contexto}" if contexto else "") + destino +
        f" REGLAS: trabaja solo dentro de {folder}. Crea y edita archivos con tus herramientas de archivos; para "
        "leer, listar o buscar usa también tus herramientas de archivos. Los comandos de terminal se deniegan en este "
        "modo: no los uses. No pegues el código completo en el chat. No instales paquetes globales. Escribe en "
        "español con ortografía correcta (tildes, ñ, ¿ y ¡). "
        + (f"Como ÚLTIMO paso obligatorio crea el archivo {resultado} con una sección '## RESUMEN' de máximo 8 viñetas: "
           "lo realizado, los archivos creados o modificados y cómo probarlo."
           if resultado else
           "Al terminar responde SOLO con una sección '## RESUMEN' de máximo 8 viñetas: lo realizado, los archivos "
           "creados o modificados y cómo probarlo.")
    )


def _agy_corte(task: dict, tipo: str, msg: str) -> str:
    """Resultado cuando agy se atasca: por límite del modelo (propone el siguiente de la cadena) o sin red."""
    model, conv = task["model"], _agy_conversacion(task["folder"])
    tokens = _agy_tokens(conv, task["inicio"]) if conv else {}
    conv_txt = f"conversacion_antigravity: {conv}\n" if conv else ""
    if tokens and any(tokens.values()):  # lo gastado antes del corte también se registra
        conv_txt += (f"Tokens: {tokens['input'] + tokens['cache_read']:,} entrada ({tokens['cache_read']:,} de caché) "
                     f"/ {tokens['output']:,} salida\n").replace(",", ".")
    if tipo == "red":
        _live(f"■ Antigravity sin conexión ({model}): {msg[:120]}")
        return (f"Sin conexión: Antigravity ({model}) no logra llegar a la API tras varios reintentos.\n"
                f"Error: {msg}\n{conv_txt}Revisa la red y vuelve a lanzar la tarea con el mismo modelo"
                + (" y esa conversación" if conv else "") + ". task_id: " + task["id"])
    descartados = [*task.get("descartados", []), model]
    siguiente = _siguiente_modelo(model, descartados)
    _live(f"■ Límite de {model} en Antigravity → siguiente: {siguiente or 'ninguno'}")
    return (f"Límite: el modelo {model} de Antigravity llegó a su límite de uso.\nError: {msg}\n"
            f"modelos_descartados: {', '.join(descartados)}\n"
            + (f"siguiente_modelo: {siguiente}\n" if siguiente else
               "siguiente_modelo: ninguno (se agotó la cadena ANTIGRAVITY_CADENA; consulta al usuario)\n")
            + conv_txt + "task_id: " + task["id"])


def _agy_conversacion(folder: Path) -> str:
    """Última conversación de agy en esa carpeta (agy la guarda en ~/.gemini/antigravity-cli/cache)."""
    f = Path.home() / ".gemini" / "antigravity-cli" / "cache" / "last_conversations.json"
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    clave = os.path.normcase(os.path.normpath(str(folder)))
    return next((v for k, v in data.items() if os.path.normcase(os.path.normpath(k)) == clave), "")


def _agy_tokens(conv: str, desde: datetime) -> dict:
    """Tokens que gastó agy en esa conversación desde `desde` (UTC): suma las respuestas del modelo del
    transcript local (~/.gemini/antigravity-cli/brain/<id>). Así una ronda de corrección no recuenta las anteriores."""
    pasos = _agy_pasos(conv, desde)
    if pasos is None:
        return {}
    tot = {"input": 0, "output": 0, "cache_read": 0}
    for ev in pasos:
        if ev.get("type") != "PLANNER_RESPONSE":
            continue
        tot["input"] += int(ev.get("input_tokens") or 0)
        tot["output"] += int(ev.get("output_tokens") or 0)
        tot["cache_read"] += int(ev.get("cache_read_tokens") or 0)
    return tot


def _agy_pasos(conv: str, desde: datetime) -> list[dict] | None:
    """Pasos del transcript local de agy (~/.gemini/antigravity-cli/brain/<id>) creados desde `desde` (UTC)."""
    if not _conv_valida(conv):
        return None
    f = (Path.home() / ".gemini" / "antigravity-cli" / "brain" / conv / ".system_generated" / "logs"
         / "transcript_full.jsonl")
    try:
        lineas = f.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    pasos = []
    for line in lineas:
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        try:
            ts = datetime.fromisoformat(str(ev.get("created_at", "")).replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
        except ValueError:
            ts = None
        if ts and ts < desde - timedelta(seconds=5):
            continue
        pasos.append(ev)
    return pasos


# agy guarda los fallos de la API como pasos ERROR_MESSAGE ("API error (attempt N): ...") y reintenta solo.
# Aún no se ha visto un límite real: se reconocen las formas habituales de la API de Google (429, cuota, etc.).
_RE_LIMITE = re.compile(r"\b429\b|RESOURCE_EXHAUSTED|quota|rate.?limit|exhausted|usage limit|limit (?:reached|exceeded)"
                        r"|too many requests|out of credits|insufficient credits|model is (?:overloaded|unavailable)",
                        re.I)
_RE_RED = re.compile(r"no such host|dial tcp|wsarecv|connection (?:refused|reset|attempt failed)|i/o timeout", re.I)
_AGY_MAX_REINTENTOS_RED = 6


def _agy_fallo(conv: str, desde: datetime) -> tuple[str, str]:
    """('limite' | 'red', mensaje) si agy está atascado por el límite del modelo o sin conexión; ('', '') si no.
    Un error seguido de una respuesta del modelo ya se superó (agy reintentó con éxito) y no cuenta."""
    pasos = _agy_pasos(conv, desde) or []
    errores = []
    for ev in pasos:
        if ev.get("type") == "PLANNER_RESPONSE":
            errores = []
        elif ev.get("type") == "ERROR_MESSAGE":
            errores.append(str(ev.get("error") or ""))
    limite = next((e for e in errores if _RE_LIMITE.search(e)), "")
    if limite:
        return "limite", limite[:300]
    red = [e for e in errores if _RE_RED.search(e)]
    if len(red) >= _AGY_MAX_REINTENTOS_RED:
        return "red", red[-1][:300]
    return "", ""


def _siguiente_modelo(model: str, descartados: list[str] | tuple = ()) -> str:
    """Siguiente modelo de ANTIGRAVITY_CADENA tras `model`, saltando los ya descartados. '' si no queda ninguno."""
    cadena = ANTIGRAVITY_CADENA
    desde = cadena.index(model) + 1 if model in cadena else 0
    return next((m for m in cadena[desde:] if m != model and m not in descartados), "")


def _conv_valida(conv: str) -> str:
    """Solo IDs con forma de UUID: el valor termina en una línea de comandos."""
    return conv if re.fullmatch(r"[0-9a-fA-F-]{8,64}", conv or "") else ""


_AGY_TASKS: dict[str, dict] = {}  # tareas preparadas, a la espera de que Claude las corra en su pestaña de terminal


async def _agy_esperar(resultado: Path, folder: Path, inicio: datetime) -> tuple[str, str, str]:
    """Espera el archivo de resultado que escribe el agente al terminar. Devuelve (texto, tipo, msg):
    tipo 'limite'/'red' si agy se atascó, 'timeout' si no terminó a tiempo, '' si todo bien."""
    limite, ultimo, vuelta = time.monotonic() + ANTIGRAVITY_TIMEOUT, -1, 0
    while time.monotonic() < limite:
        await asyncio.sleep(2)
        if resultado.exists():
            size = resultado.stat().st_size
            if size and size == ultimo:  # espera a que termine de escribirse
                texto = resultado.read_text(encoding="utf-8", errors="replace").strip()
                resultado.unlink(missing_ok=True)
                return texto, "", ""
            ultimo = size
            continue
        vuelta += 1
        if vuelta % 5 == 0:  # cada ~10 s revisa si agy quedó atascado por el límite del modelo o sin red
            tipo, msg = _agy_fallo(_agy_conversacion(folder), inicio)
            if tipo:
                return "", tipo, msg
        if vuelta % 15 == 0:
            _live(f"  … esperando a Antigravity ({vuelta * 2} s)")
    return "", "timeout", f"Antigravity no escribió {resultado.name} en {ANTIGRAVITY_TIMEOUT}s"


async def _agy_resultado(task: dict, texto: str) -> str:
    """Resumen de una tarea terminada: archivos tocados, tokens, log y síntesis."""
    folder, model = task["folder"], task["model"]
    after = await asyncio.to_thread(_snapshot, folder)
    changed = sorted(p for p, m in after.items() if task["before"].get(p) != m and Path(p).name != task["resultado"].name)
    log_dir = LOGS_DIR / "personal"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"{task['id']}.md"
    log_file.write_text(f"# Antigravity ({model})\n\n- carpeta: {folder}\n\n## Tarea\n\n{task['instruccion']}\n\n"
                        "## Archivos\n\n" + "\n".join(f"- {p}" for p in changed) + f"\n\n## Resultado\n\n{texto}\n",
                        encoding="utf-8")
    m = re.search(r"##\s*RESUMEN\s*(.+)$", texto, flags=re.S | re.I)
    resumen = m.group(1).strip() if m else _tail(texto, 800)
    shown = "\n".join(f"  - {p}" for p in changed[:15]) + (f"\n  … y {len(changed) - 15} más" if len(changed) > 15 else "")
    conv_id = _agy_conversacion(folder)
    tokens = _agy_tokens(conv_id, task["inicio"]) if conv_id else {}
    tok_txt = (f"Tokens: {tokens['input'] + tokens['cache_read']:,} entrada ({tokens['cache_read']:,} de caché) "
               f"/ {tokens['output']:,} salida\n".replace(",", ".") if tokens else "")
    _live(f"■ Éxito con {model} | {len(changed)} archivos | {tok_txt.strip()} | síntesis devuelta a Claude")
    return (f"Éxito: Antigravity ({model}, CLI oficial) completó la tarea en {folder}.\n"
            f"Archivos creados/modificados: {len(changed)}\n{shown}\n{tok_txt}Log: {log_file}\n"
            + (f"conversacion_antigravity: {conv_id} (pásala en una ronda de corrección para conservar el contexto)\n"
               if conv_id else "")
            + "La pestaña de Antigravity sigue abierta por si quieres continuar la conversación ahí.\n"
            f"\nSíntesis:\n{_tail(resumen)}")


@mcp.tool()
async def preparar_tarea_antigravity(
    instruccion: str,
    contexto_adicional: str = "",
    ruta_salida_opcional: str = "",
    modelo_antigravity: str = "",
    pedido_id: str = "",
    item: int = 0,
    conversacion: str = "",
    modelos_descartados: str = "",
) -> str:
    """PERSONAL (fuera de TRABAJO_DIR) — Paso 1 de 3: prepara la tarea y devuelve `task_id` y el comando del CLI de Antigravity.
    Paso 2: ejecútalo con `run_in_terminal` (título 'Antigravity', sin cwd) y muestra la pestaña con `show_pane`.
    Paso 3: llama `esperar_resultado_antigravity(task_id)`. En correcciones reutiliza `conversacion`.
    Modelos (`modelo_antigravity`): `gemini-3.8-flash-low` simple; vacío = flash-medium; `gemini-4-argon-medium` varios archivos; `gemini-4-argon-high` muy exigente."""
    target = _resolve_personal(ruta_salida_opcional or "_antigravity")
    if not _inside(target, PERSONAL_DIR, UNIVERSIDAD_DIR, *OTRAS_CARPETAS):
        return (f"Rechazado: {target} está fuera de {PERSONAL_DIR} y {UNIVERSIDAD_DIR}. Si es un proyecto de "
                "trabajo usa preparar_vista_opencode + ejecutar_trabajo_opencode_innti.")
    agy = _agy_cmd()
    if not agy:
        return f"Error: no se encontró agy en '{ANTIGRAVITY_CLI_PATH}'. Revisa ANTIGRAVITY_CLI_PATH en .env."
    folder = target.parent if target.suffix else target
    folder.mkdir(parents=True, exist_ok=True)
    model = (modelo_antigravity or ANTIGRAVITY_AGY_MODEL).strip()
    _agy_confiar(folder)
    aviso_item = ""
    if not (pedido_id and item):  # ajuste sin pedido: queda como ítem nuevo del pedido del proyecto
        pedido_id, item, aviso_item = registro.item_automatico(str(folder), instruccion)
    task_id = f"{_stamp()}-{_slug(instruccion, 24)}-{os.urandom(2).hex()}"
    resultado = folder / f".resultado-{task_id}.md"
    # El prompt completo (con tildes y sin límite de tamaño) va en un archivo: el comando de la pestaña de Claude solo
    # admite ASCII y 2000 bytes.
    prompt_file = registro.TAREAS_DIR / "prompts" / f"{task_id}.md"
    prompt_file.parent.mkdir(parents=True, exist_ok=True)
    prompt_file.write_text(_agy_prompt(instruccion, contexto_adicional, target, folder, resultado), encoding="utf-8")
    task = {"id": task_id, "folder": folder, "resultado": resultado, "model": model, "instruccion": instruccion,
            "target": target, "pedido_id": pedido_id, "item": item, "inicio": datetime.now(timezone.utc),
            "before": await asyncio.to_thread(_snapshot, folder), "prompt_file": prompt_file,
            "descartados": [m.strip() for m in modelos_descartados.split(",") if m.strip()]}
    _AGY_TASKS[task_id] = task
    if pedido_id and item:
        registro.actualizar_item(pedido_id, item, "en_curso", agente="Antigravity", modelo=model)
    _live("")
    _live(f"▶ PERSONAL → Antigravity (pestaña de Claude) | {folder} | {model}")
    _live(f"  instrucción: {instruccion[:300]}")
    conv = ["--conversation", conversacion] if _conv_valida(conversacion) else []
    args = [*conv, "-i", plataforma.quote_prompt(f"Lee el archivo {prompt_file} y ejecuta la tarea que describe, tal cual."),
            "--model", model, "--mode", "accept-edits"]
    if ANTIGRAVITY_SIN_PERMISOS:
        args.append("--dangerously-skip-permissions")
    # Entra primero a la carpeta personal: agy toma el directorio actual como espacio de trabajo.
    cmd = plataforma.run_line(agy, *args, cwd=folder)
    return (f"task_id: {task_id}\ncarpeta: {folder}{aviso_item}\n"
            f"comando (ejecútalo con run_in_terminal, una sola línea, sin cambios; luego muestra la pestaña con show_pane):\n{cmd}")


@mcp.tool()
async def esperar_resultado_antigravity(task_id: str) -> str:
    """PERSONAL — Paso 3 de 3: espera a que Antigravity termine la tarea preparada y devuelve la síntesis.
    Si responde 'Límite:', relanza el ítem con `preparar_tarea_antigravity` usando `siguiente_modelo`, `conversacion` y `modelos_descartados`."""
    task = _AGY_TASKS.get(task_id)
    if not task:
        return f"Error: no hay una tarea preparada con task_id '{task_id}' en este servidor."
    texto, tipo, msg = await _agy_esperar(task["resultado"], task["folder"], task["inicio"])
    if tipo == "timeout":
        task["inicio"] = datetime.now(timezone.utc)
        return (f"Sin resultado: {msg}. Su pestaña sigue abierta; revisa si espera una aprobación o si sigue trabajando.\n"
                f"task_id: {task_id}")
    _AGY_TASKS.pop(task_id, None)
    res = _agy_corte(task, tipo, msg) if tipo else await _agy_resultado(task, texto)
    out = _cierre_personal(res, task["pedido_id"], task["item"], task["model"], task["folder"])
    if res.startswith("Límite") and "siguiente_modelo: ninguno" not in res:
        out += ("\nQué hacer: cierra la pestaña de Antigravity (stop_terminal_tab) y relanza el mismo ítem con "
                "preparar_tarea_antigravity(misma instrucción, modelo_antigravity=<siguiente_modelo>, "
                "conversacion=<conversacion_antigravity>, modelos_descartados=<modelos_descartados>, pedido_id, item). "
                "Avisa al usuario en una línea del cambio de modelo.")
    return out


# ----------------------------------------------------------------------------- verificación

@mcp.tool()
async def verificar_proyecto(ruta: str, comandos: list[str] | None = None) -> str:
    """Corre los tests/lint del proyecto que contiene `ruta` y devuelve VERDE/ROJO con el final de cada salida.
    Usa los comandos guardados con configurar_verificacion o los detecta (Maven, Gradle, npm, pytest, Go).
    Úsala solo si el usuario lo pide o el cambio es riesgoso (no por defecto en cada revisión)."""
    target = _resolve_under(TRABAJO_DIR, ruta)
    if not _inside(target, TRABAJO_DIR, PERSONAL_DIR, UNIVERSIDAD_DIR, *OTRAS_CARPETAS):
        return f"Rechazado: {target} está fuera de las carpetas configuradas."
    tope = next((b for b in (TRABAJO_DIR, PERSONAL_DIR, UNIVERSIDAD_DIR) if _inside(target, b)), TRABAJO_DIR)
    _live(f"▶ VERIFICACIÓN en {target}")
    res = await control.verificar(target, tope, comandos, LOGS_DIR / "verificacion")
    _live(f"■ {res.splitlines()[0]}")
    return res


@mcp.tool()
def configurar_verificacion(proyecto: str, comandos: list[str]) -> str:
    """Guarda cómo se verifica un proyecto (nombre de la carpeta de primer nivel o ruta absoluta), p. ej.
    configurar_verificacion('ssdd-back', ['mvn -q -B test']). Lista vacía = quitar la configuración."""
    return control.guardar_verificacion(proyecto, comandos)


# ----------------------------------------------------------------------------- checklist, costos y aprendizajes

@mcp.tool()
def iniciar_pedido(titulo: str, items: list[str], contexto: str = "", ruta: str = "") -> str:
    """Crea el checklist de un pedido del usuario (un ítem por entregable/delegación/verificación).
    titulo: nombre corto del pedido, SIN rutas (p. ej. 'Nexus SaaS – gestión de proyectos').
    ruta: carpeta del proyecto, aparte (se muestra al abrir el pedido en el panel).
    Devuelve pedido_id: pásalo con el número de ítem a las herramientas de delegación."""
    return registro.iniciar_pedido(titulo, items, contexto, ruta)


@mcp.tool()
def actualizar_item(pedido_id: str, item: int, estado: str, nota: str = "") -> str:
    """Cambia el estado de un ítem del checklist (item 1 = primero).
    estado: pendiente | en_curso | revisando | corrigiendo | hecho | fallido | omitido.
    'corrigiendo' suma una ronda; si se pasa de MAX_RONDAS_CORRECCION avisa que hay que consultar al usuario.
    nota: veredicto de tu revisión (qué estaba bien/mal)."""
    return registro.actualizar_item(pedido_id, item, estado, nota)


@mcp.tool()
def agregar_items(pedido_id: str, items: list[str]) -> str:
    """Agrega ítems a un checklist existente (p. ej. mejoras detectadas en la revisión)."""
    return registro.agregar_items(pedido_id, items)


@mcp.tool()
def cerrar_pedido(pedido_id: str, resumen: str, estado: str = "cerrado") -> str:
    """Cierra el pedido con un resumen final (estado: cerrado | cancelado | bloqueado)."""
    return registro.cerrar_pedido(pedido_id, resumen, estado)


@mcp.tool()
def ver_pedido(pedido_id: str = "") -> str:
    """Muestra el checklist de un pedido (vacío = el más reciente)."""
    return registro.ver_pedido(pedido_id)


@mcp.tool()
def listar_pedidos(solo_abiertos: bool = True) -> str:
    """Lista los pedidos registrados (por defecto solo los abiertos), para retomar trabajo pendiente."""
    return registro.listar_pedidos(solo_abiertos)


@mcp.tool()
def resumen_costos(dias: int = 1) -> str:
    """Consumo de las delegaciones de los últimos N días: tokens y costo estimado por agente y modelo."""
    return registro.resumen_costos(dias)


@mcp.tool()
def registrar_aprendizaje(ruta_o_proyecto: str, agente: str, modelo: str, resultado: str, nota: str) -> str:
    """Tras revisar una delegación, guarda qué tal funcionó el modelo en ese proyecto.
    resultado: bien | regular | mal. nota: una línea concreta (p. ej. 'rompió tests de integración')."""
    return registro.registrar_aprendizaje(ruta_o_proyecto, agente, modelo, resultado, nota)


@mcp.tool()
def consultar_aprendizajes(ruta_o_proyecto: str = "") -> str:
    """Antes de elegir modelo para un proyecto, consulta qué funcionó bien o mal en él."""
    return registro.consultar_aprendizajes(ruta_o_proyecto)


@mcp.tool()
def exportar_panel() -> str:
    """Prepara la sincronización del panel de control (artefacto). Devuelve la llamada ArtifactData 'batch'
    exacta a ejecutar: crea un documento de estado nuevo y borra el anterior (sin leer nada antes)."""
    data = registro.datos_panel()
    doc_id = f"s-{datetime.now():%Y%m%d%H%M%S}"
    sync_dir = registro.TAREAS_DIR / "panel"
    sync_dir.mkdir(parents=True, exist_ok=True)
    path = sync_dir / f"{doc_id}.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    state_file = sync_dir / "ultimo.txt"
    prev = state_file.read_text(encoding="utf-8").strip() if state_file.exists() else ""
    state_file.write_text(doc_id, encoding="utf-8")
    if prev and prev != doc_id:
        (sync_dir / f"{prev}.json").unlink(missing_ok=True)
    writes = [{"op": "set", "collection": "estado", "doc_id": doc_id, "file_path": str(path)}]
    if prev and prev != doc_id:
        writes.append({"op": "delete", "collection": "estado", "doc_id": prev, "if_version": 1})
    url = PANEL_URL or "<PANEL_URL sin configurar en .env>"
    return (f"Ejecuta ArtifactData action='batch' url='{url}' writes={json.dumps(writes, ensure_ascii=False)}\n"
            f"({len(data['pedidos'])} pedidos · si el delete falla por versión, repite solo el set.)")


if __name__ == "__main__":
    _podar_logs()
    mcp.run()
