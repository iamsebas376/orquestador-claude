"""
MCP Orchestrator: Claude actúa como cerebro/enrutador y delega la ejecución pesada.

  TRABAJO  -> OpenCode CLI usando modelos Innti (proveedor LiteLLM corporativo)
  PERSONAL -> Antigravity (modo "api": modelo Gemini vía endpoint OpenAI-compatible;
                           modo "cli": abre la tarea en el agente del IDE Antigravity)

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
import unicodedata
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
LOGS_DIR = Path(_env("LOGS_DIR") or plataforma.REPO_ROOT / "logs").expanduser()

OPENCODE_CLI_PATH = _env("OPENCODE_CLI_PATH", "opencode")
OPENCODE_CONFIG_PATH = Path(_env("OPENCODE_CONFIG_PATH") or plataforma.default_opencode_config()).expanduser()
OPENCODE_TIMEOUT = int(_env("OPENCODE_TIMEOUT_SECONDS", "1800"))
# Servidor OpenCode persistente: las tareas corren en él y una ventana con su TUI muestra la sesión en vivo.
OPENCODE_SERVER_URL = _env("OPENCODE_SERVER_URL", "http://127.0.0.1:4096").rstrip("/")
SHOW_CONSOLE = _env("MOSTRAR_CONSOLA", "1") not in ("0", "false", "no")
INNTI_PROVIDER = _env("INNTI_PROVIDER", "litellm-proxy")
INNTI_DEFAULT_MODEL = _env("INNTI_DEFAULT_MODEL", "innti-dsp")
INNTI_BASE_URL = _env("INNTI_BASE_URL").rstrip("/")
INNTI_API_KEY = _env("INNTI_API_KEY")

# agy = CLI oficial de Antigravity (cuenta del usuario, plan Pro); api = respaldo con la API de Gemini.
ANTIGRAVITY_MODE = _env("ANTIGRAVITY_MODE", "agy").lower()
ANTIGRAVITY_CLI_PATH = _env("ANTIGRAVITY_CLI_PATH") or plataforma.default_agy()
ANTIGRAVITY_AGY_MODEL = _env("ANTIGRAVITY_AGY_MODEL", "gemini-3.8-flash-medium")
ANTIGRAVITY_AGY_FALLBACK = [
    m.strip() for m in _env("ANTIGRAVITY_AGY_FALLBACK", "gemini-3.8-flash-low").split(",") if m.strip()
]
# Orden de respaldo cuando un modelo llega a su límite: se pasa al siguiente de la lista (de más a menos capaz).
ANTIGRAVITY_CADENA = [
    m.strip() for m in _env(
        "ANTIGRAVITY_CADENA", "gemini-4-argon-high,gemini-4-argon-medium,gemini-3.8-flash-medium,gemini-3.8-flash-low"
    ).split(",") if m.strip()
]
ANTIGRAVITY_SKIP_PERMISSIONS = _env("ANTIGRAVITY_SKIP_PERMISSIONS", "0") in ("1", "true", "si", "sí")
# tui = la tarea corre en la TUI oficial de agy en su ventana; visor = agy sin ventana + visor propio.
ANTIGRAVITY_VISTA = _env("ANTIGRAVITY_VISTA", "tui").lower()
ANTIGRAVITY_API_KEY = _env("ANTIGRAVITY_API_KEY")
ANTIGRAVITY_BASE_URL = _env(
    "ANTIGRAVITY_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai"
).rstrip("/")
ANTIGRAVITY_MODEL = _env("ANTIGRAVITY_MODEL", "gemini-3.5-flash")
ANTIGRAVITY_FALLBACK_MODELS = [
    m.strip() for m in _env("ANTIGRAVITY_FALLBACK_MODELS", "gemini-flash-latest,gemini-3.1-flash-lite").split(",")
    if m.strip()
]
ANTIGRAVITY_TIMEOUT = int(_env("ANTIGRAVITY_TIMEOUT_SECONDS", "600"))

MAX_RETURN_CHARS = int(_env("MAX_RETURN_CHARS", "1500"))
AISLAR_TRABAJO = _env("AISLAR_TRABAJO", "1") not in ("0", "false", "no")
PANEL_URL = _env("PANEL_URL")
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "dist", "build", "target", ".next", "__pycache__", ".angular",
             ".pytest_cache", ".mypy_cache", ".ruff_cache", ".gradle", ".idea", ".vscode"}

mcp = FastMCP("orchestrator")


# ----------------------------------------------------------------------------- utilidades

LIVE_LOG = LOGS_DIR / "en-vivo.log"


def _live(msg: str) -> None:
    """Registro de texto en tiempo real (logs/en-vivo.log): Get-Content -Wait (Windows) o tail -f (macOS)."""
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


def _strip_jsonc(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    text = re.sub(r'(?m)^(\s*)//.*$', r"\1", text)
    return re.sub(r",(\s*[}\]])", r"\1", text)


def _opencode_cmd() -> str | None:
    if Path(OPENCODE_CLI_PATH).is_file():
        return OPENCODE_CLI_PATH
    return shutil.which(OPENCODE_CLI_PATH)


# Incluye OPENCODE_SERVER_PASSWORD (cargada del .env): serve exige la contraseña y run/attach la envían.
OPENCODE_ENV = {**os.environ, "OPENCODE_DISABLE_CLAUDE_CODE_PROMPT": "1"}


def _oc_auth() -> tuple[str, str] | None:
    """Basic auth para la API del servidor OpenCode (usuario por defecto 'opencode')."""
    pwd = os.getenv("OPENCODE_SERVER_PASSWORD")
    return (os.getenv("OPENCODE_SERVER_USERNAME") or "opencode", pwd) if pwd else None


def _open_console(title: str, cmd: list[str], cwd: Path) -> None:
    """Abre una ventana de terminal externa (Windows Terminal, Terminal.app o Linux) con el comando dado."""
    if not SHOW_CONSOLE:
        return
    if err := plataforma.open_terminal(title, cmd, cwd, OPENCODE_ENV):
        _live(f"  ✖ no se pudo abrir la consola: {err}")


async def _ensure_opencode_server(cli: str) -> bool:
    """Arranca `opencode serve` en segundo plano si no responde. Devuelve True si queda disponible."""
    async with httpx.AsyncClient(timeout=2, auth=_oc_auth()) as c:
        for intento in range(30):
            try:
                if (await c.get(f"{OPENCODE_SERVER_URL}/project")).status_code < 500:
                    return True
            except httpx.HTTPError:
                pass
            if intento == 0:
                port = OPENCODE_SERVER_URL.rsplit(":", 1)[-1]
                _live(f"  iniciando servidor OpenCode en el puerto {port}…")
                subprocess.Popen(
                    [cli, "serve", "--port", port], cwd=str(TRABAJO_DIR), env=OPENCODE_ENV,
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            await asyncio.sleep(0.5)
    return False


async def _create_session(cwd: Path, title: str) -> str | None:
    try:
        async with httpx.AsyncClient(timeout=10, auth=_oc_auth()) as c:
            r = await c.post(f"{OPENCODE_SERVER_URL}/session", params={"directory": str(cwd)}, json={"title": title})
            r.raise_for_status()
            return r.json()["id"]
    except (httpx.HTTPError, KeyError, ValueError) as e:
        _live(f"  ✖ no se pudo crear la sesión en el servidor: {e}")
        return None


async def _run(cmd: list[str], cwd: Path, timeout: int) -> tuple[int, str]:
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

    async def pump() -> None:
        assert proc.stdout
        async for raw in proc.stdout:
            line = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", raw.decode("utf-8", errors="replace")).rstrip()
            lines.append(line)
            if line:
                _live(f"  │ {line}")

    try:
        await asyncio.wait_for(pump(), timeout=timeout)
        await proc.wait()
    except asyncio.TimeoutError:
        proc.kill()
        _live(f"  ✖ timeout tras {timeout}s")
        return -1, f"Timeout tras {timeout}s"
    return proc.returncode or 0, "\n".join(lines)


# ----------------------------------------------------------------------------- herramientas

@mcp.tool()
async def listar_modelos_innti() -> str:
    """Lista los modelos Innti configurados en OpenCode (proveedor INNTI_PROVIDER).
    Úsala para elegir el modelo según la complejidad antes de delegar trabajo."""
    modelos: list[str] = []
    fuente = ""

    # 1) Configuración de OpenCode (rápido y sin red)
    if OPENCODE_CONFIG_PATH.is_file():
        try:
            cfg = json.loads(_strip_jsonc(OPENCODE_CONFIG_PATH.read_text(encoding="utf-8")))
            prov = cfg.get("provider", {}).get(INNTI_PROVIDER, {})
            for name, spec in (prov.get("models") or {}).items():
                variants = list((spec or {}).get("variants", {}).keys())
                modelos.append(name + (f" (variantes: {', '.join(variants)})" if variants else ""))
            fuente = str(OPENCODE_CONFIG_PATH)
        except Exception as e:  # noqa: BLE001
            fuente = f"error leyendo config: {e}"

    # 2) CLI de OpenCode
    if not modelos and (cli := _opencode_cmd()):
        code, out = await _run([cli, "models", INNTI_PROVIDER], BASE_DIR, 60)
        if code == 0:
            modelos = [l.strip() for l in out.splitlines() if l.strip()]
            fuente = "opencode models"

    # 3) Endpoint /models de LiteLLM
    if not modelos and INNTI_BASE_URL and INNTI_API_KEY:
        try:
            async with httpx.AsyncClient(timeout=20) as c:
                r = await c.get(f"{INNTI_BASE_URL}/models", headers={"Authorization": f"Bearer {INNTI_API_KEY}"})
                r.raise_for_status()
                modelos = sorted(m["id"] for m in r.json().get("data", []) if "innti" in m["id"].lower())
                fuente = f"{INNTI_BASE_URL}/models"
        except Exception as e:  # noqa: BLE001
            fuente = f"error consultando API: {e}"

    if not modelos:
        return f"No se encontraron modelos Innti. Fuente: {fuente or 'ninguna disponible'}"
    lista = "\n".join(f"- {INNTI_PROVIDER}/{m}" for m in modelos)
    _live(f"■ listar_modelos_innti → {len(modelos)} modelos ({fuente})")
    return f"Modelos Innti (por defecto: {INNTI_DEFAULT_MODEL}). Fuente: {fuente}\n{lista}"


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


async def _session_tokens(session_id: str, cwd: Path, desde_ms: int) -> dict:
    """Suma los tokens de los mensajes del asistente creados en esta ejecución de la sesión de OpenCode."""
    tot = {"input": 0, "output": 0, "reasoning": 0, "cache_read": 0}
    try:
        async with httpx.AsyncClient(timeout=10, auth=_oc_auth()) as c:
            r = await c.get(f"{OPENCODE_SERVER_URL}/session/{session_id}/message", params={"directory": str(cwd)})
            r.raise_for_status()
            for msg in r.json():
                info = msg.get("info") or {}
                if info.get("role") != "assistant" or (info.get("time") or {}).get("created", 0) < desde_ms:
                    continue
                t = info.get("tokens") or {}
                tot["input"] += t.get("input", 0)
                tot["output"] += t.get("output", 0)
                tot["reasoning"] += t.get("reasoning", 0)
                tot["cache_read"] += (t.get("cache") or {}).get("read", 0)
    except (httpx.HTTPError, ValueError):
        pass
    return tot


def _work_cwd(target: Path) -> Path:
    """Carpeta de trabajo de OpenCode para una ruta (archivo → su carpeta), creándola si no existe."""
    if target.suffix and not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        return target.parent
    if target.is_file():
        return target.parent
    target.mkdir(parents=True, exist_ok=True)
    return target


def _aislar(target: Path, aislar: bool | None, texto: str) -> tuple[Path, dict | None, str]:
    """Si corresponde, mueve la tarea a un worktree aislado. Devuelve (target, info del worktree, aviso)."""
    info = control.worktree_info(target)
    if info:  # ya está en un worktree del orquestador (ronda de corrección)
        return target, info, ""
    if not (AISLAR_TRABAJO if aislar is None else aislar):
        return target, None, ""
    try:
        nuevo = control.crear_worktree(target, f"{datetime.now():%m%d-%H%M%S}-{_slug(texto, 20)}", TRABAJO_DIR)
    except (RuntimeError, OSError, subprocess.SubprocessError) as e:
        return target, None, f"(No se pudo aislar: {e}. Se trabaja directo en el repo.)\n"
    if not nuevo:  # no es un repo git (o no tiene commits): se trabaja directo
        return target, None, ""
    _live(f"  aislado en {nuevo['worktree']} (rama {nuevo['rama']})")
    return nuevo["target"], control.worktree_info(nuevo["worktree"]), ""


@mcp.tool()
async def preparar_vista_opencode(ruta_proyecto_o_archivo: str = "", titulo: str = "",
                                  aislar: bool | None = None) -> str:
    """SOLO EN CLAUDE CODE (terminal interna). Paso 1 de 2 para una tarea de TRABAJO.

    Crea la sesión en el servidor de OpenCode (en un worktree aislado si es repo git y AISLAR_TRABAJO=1) y
    devuelve session_id, ruta_para_ejecutar y el comando de la TUI oficial. Ejecuta el comando tal cual con
    run_in_terminal (título 'OpenCode'), y luego llama ejecutar_trabajo_opencode_innti con
    sesion_opencode=<session_id> y ruta_proyecto_o_archivo=<ruta_para_ejecutar>.
    """
    target = _resolve_under(TRABAJO_DIR, ruta_proyecto_o_archivo)
    if not _inside(target, TRABAJO_DIR):
        return (f"Rechazado: {target} está fuera de {TRABAJO_DIR}. Si es un proyecto personal o de "
                "universidad usa preparar_tarea_antigravity.")
    cli = _opencode_cmd()
    if not cli:
        return f"Error: no se encontró OpenCode ('{OPENCODE_CLI_PATH}'). Revisa OPENCODE_CLI_PATH en .env."
    if not await _ensure_opencode_server(cli):
        return f"Error: el servidor de OpenCode no respondió en {OPENCODE_SERVER_URL}."
    target, wt, aviso = _aislar(target, aislar, titulo or target.name)
    cwd = _work_cwd(target)
    session_id = await _create_session(cwd, f"Claude → {titulo or cwd.name}"[:80])
    if not session_id:
        return "Error: no se pudo crear la sesión en el servidor de OpenCode."
    # oc_attach.py lee la contraseña del .env: no aparece en el comando ni en el chat.
    cmd = plataforma.run_line(sys.executable, plataforma.quote_path(BASE_DIR / "oc_attach.py"),
                              "--dir", plataforma.quote_path(cwd), "--session", session_id)
    aislado = (f"Trabajo aislado: rama {wt['rama']} en el worktree {wt['worktree']} (tu repo no se toca).\n"
               if wt else "")
    return (f"session_id: {session_id}\nruta_para_ejecutar: {target}\n{aislado}{aviso}"
            f"comando (ejecútalo con run_in_terminal, una sola línea, sin cambios; luego muestra la pestaña con show_pane):\n{cmd}")


@mcp.tool()
async def ejecutar_trabajo_opencode_innti(
    prompt_tarea: str,
    modelo_innti: str = "",
    ruta_proyecto_o_archivo: str = "",
    sesion_opencode: str = "",
    pedido_id: str = "",
    item: int = 0,
    aislar: bool | None = None,
    forzar_presupuesto: bool = False,
) -> str:
    """CONTEXTO TRABAJO (Innti/corporativo). Delega la tarea a OpenCode con un modelo Innti.
    OpenCode edita los archivos directamente; aquí solo vuelve un resumen de estado, tokens y costo.

    aislar: en repos git trabaja en un worktree con rama orq/… (por defecto AISLAR_TRABAJO=1). Para una ronda
        de corrección pasa como ruta el mismo worktree. Al aprobar: integrar_trabajo(worktree, 'aplicar').
    forzar_presupuesto: solo si el usuario autorizó pasar el límite de presupuesto.

    sesion_opencode: session_id de preparar_vista_opencode (TUI en la terminal de Claude) o de una delegación
        anterior: reutilizarla en una RONDA DE CORRECCIÓN conserva el contexto del agente y cuesta menos.
        Vacío = sesión nueva con ventana de Windows Terminal (pestaña Chat).
    pedido_id, item: vinculan la delegación al checklist (el ítem pasa a 'revisando' y suma el costo).
    prompt_tarea: instrucción completa y autocontenida para OpenCode.
    modelo_innti: vacío = INNTI_DEFAULT_MODEL (innti-dsp: lógica, revisión, seguridad).
        'innti-dsf-last' explorar/cambios mecánicos (barato), 'innti-dev' documentación/commits,
        'innti-gpt' planificar o con imágenes, 'innti-nova-flash' capturas sencillas,
        'innti-terra' arquitectura con respuestas cortas (salida cara).
    ruta_proyecto_o_archivo: proyecto dentro de TRABAJO_DIR, absoluto (p. ej. <TRABAJO_DIR>/mi-proyecto)
        o relativo ('ssdd-back\\src\\...'). Rutas fuera de TRABAJO_DIR se rechazan.
    """
    target = _resolve_under(TRABAJO_DIR, ruta_proyecto_o_archivo)
    _live("")
    _live(f"▶ TRABAJO → OpenCode | ruta: {target}")
    _live(f"  prompt: {prompt_tarea[:300]}")
    if not _inside(target, TRABAJO_DIR):
        _live(f"  ✖ rechazado: ruta fuera de {TRABAJO_DIR}")
        return (f"Rechazado: {target} está fuera de {TRABAJO_DIR}. Si es un proyecto personal o de "
                "universidad usa ejecutar_personal_antigravity.")

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

    aviso_item = ""
    if not (pedido_id and item):  # ajuste sin pedido: queda como ítem nuevo del pedido del proyecto
        pedido_id, item, aviso_item = registro.item_automatico(str(target if not target.suffix else target.parent),
                                                               prompt_tarea)
    target, wt, aviso_aislar = _aislar(target, aislar, prompt_tarea)
    cwd = _work_cwd(target)
    if pedido_id and item:
        registro.actualizar_item(pedido_id, item, "en_curso", agente="OpenCode", modelo=modelo.split("/")[-1])

    prompt = prompt_tarea
    if target.suffix:
        prompt = f"Archivo objetivo: {target}\n\n{prompt_tarea}"
    prompt += ("\n\nNo instales paquetes globales ni cambies la configuración del sistema; si falta una "
               "dependencia, usa la del proyecto o indícalo en el resumen."
               "\nEscribe los cambios directamente en disco. Al final responde SOLO con un "
               "resumen breve (máx. 10 líneas) de lo realizado y los archivos tocados.")

    _live(f"  modelo: {modelo} | ejecutando opencode run en {cwd}")
    cmd = [cli, "run", "--model", modelo]
    titulo = f"Claude → {prompt_tarea[:60]}"
    session_id = ""
    if sesion_opencode and await _ensure_opencode_server(cli):
        # TUI ya abierta (terminal de Claude o ventana previa) o ronda de corrección: misma sesión.
        session_id = sesion_opencode
        cmd += ["--attach", OPENCODE_SERVER_URL, "--dir", str(cwd), "--session", session_id]
    elif await _ensure_opencode_server(cli) and (session_id := await _create_session(cwd, titulo) or ""):
        # La ventana muestra la TUI real de OpenCode siguiendo esta sesión mientras trabaja.
        _open_console(f"OpenCode · {modelo.split('/')[-1]}",
                      [cli, "attach", OPENCODE_SERVER_URL, "--dir", str(cwd), "--session", session_id], cwd)
        await asyncio.sleep(2)
        cmd += ["--attach", OPENCODE_SERVER_URL, "--dir", str(cwd), "--session", session_id]
    else:
        _live("  servidor OpenCode no disponible: se ejecuta sin ventana")
    checkpoint = "" if wt else _git_checkpoint(cwd)
    before = _snapshot(cwd)
    inicio_ms = int(time.time() * 1000)
    code, output = await _run([*cmd, prompt], cwd, OPENCODE_TIMEOUT)
    after = _snapshot(cwd)
    changed = sorted(p for p, m in after.items() if before.get(p) != m)
    tokens = await _session_tokens(session_id, cwd, inicio_ms) if session_id else {}
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
    if wt:
        w = wt["worktree"]
        sha = control.confirmar_cambios(Path(w), f"orq: {prompt_tarea[:80]}")
        revisar = (f"Trabajo aislado en la rama {wt['rama']} (worktree {w}); tu repo no se tocó.\n"
                   f"Esta ronda: git -C \"{w}\" show --stat {sha or 'HEAD'} · todo: git -C \"{w}\" diff {wt['base'][:10]}\n"
                   f"Verifica con verificar_proyecto(\"{target}\"). Ronda de corrección: misma ruta y sesion_opencode. "
                   f"Al aprobar: integrar_trabajo(\"{w}\", \"aplicar\") (cambios en staging, sin commit) o \"descartar\".")
    else:
        revisar = f"Revisa con: git -C \"{cwd}\" diff" if checkpoint else "Revisa los archivos listados."
    _, presupuesto = control.estado_presupuesto()
    alerta = f"\n{presupuesto}" if presupuesto.startswith(("⚠", "⛔")) else ""
    return (
        f"{estado}: OpenCode usando el modelo {modelo} de Innti en {target}.\n"
        f"Archivos modificados: {len(changed)} | Líneas procesadas: {_count_lines(changed)} | Tokens: {tok_txt}\n"
        f"{shown}\n"
        + (f"sesion_opencode: {session_id} (pásala en una ronda de corrección para conservar el contexto)\n"
           if session_id else "")
        + (f"Punto de retorno: {checkpoint}\n" if checkpoint else "")
        + aviso_aislar
        + f"{revisar}\nLog completo: {log_file}{vinculo}{alerta}\n\nResumen de OpenCode:\n{_tail(output)}"
    )


@mcp.tool()
async def ejecutar_personal_antigravity(
    instruccion: str,
    contexto_adicional: str = "",
    ruta_salida_opcional: str = "",
    modelo_antigravity: str = "",
    pedido_id: str = "",
    item: int = 0,
    conversacion: str = "",
) -> str:
    """CONTEXTO PERSONAL (proyectos propios, aprendizaje, automatizaciones caseras, análisis).
    Delega al agente de Antigravity (CLI `agy`, cuenta Pro): trabaja en la carpeta, crea/edita archivos
    y aquí vuelve solo una síntesis. En Claude Code prefiere preparar_tarea_antigravity (terminal interna).

    conversacion: en una ronda de corrección, la conversacion_antigravity del resultado anterior.

    pedido_id, item: vinculan la delegación al checklist (el ítem pasa a 'revisando').

    modelo_antigravity: elige según la tarea (vacío = ANTIGRAVITY_AGY_MODEL, gemini-3.8-flash-medium):
        'gemini-3.8-flash-low'    → simple: resúmenes, listas, reformatear, preguntas rápidas.
        'gemini-3.8-flash-medium' → normal: código personal, planes, explicaciones.
        'gemini-4-argon-medium'   → complejo: proyectos con varios archivos, diseño, depuración difícil.
        'gemini-4-argon-high'     → profundo: razonamiento largo o análisis exigente (más lento).
    instruccion: la tarea a realizar.
    contexto_adicional: datos, restricciones o contexto de apoyo.
    ruta_salida_opcional: archivo o carpeta dentro de PERSONAL_DIR o UNIVERSIDAD_DIR. Relativa:
        'universidad/curso/...' -> UNIVERSIDAD_DIR/curso/...; cualquier otra -> PERSONAL_DIR/...
        Vacía -> PERSONAL_DIR/_antigravity.
    """
    target = _resolve_personal(ruta_salida_opcional or "_antigravity")
    _live("")
    _live(f"▶ PERSONAL → Antigravity | salida: {target}")
    _live(f"  instrucción: {instruccion[:300]}")
    if not _inside(target, PERSONAL_DIR, UNIVERSIDAD_DIR):
        _live(f"  ✖ rechazado: ruta fuera de {PERSONAL_DIR} y {UNIVERSIDAD_DIR}")
        return (f"Rechazado: {target} está fuera de {PERSONAL_DIR} y {UNIVERSIDAD_DIR}. Si es un "
                "proyecto de trabajo usa ejecutar_trabajo_opencode_innti.")

    folder = target.parent if target.suffix else target
    folder.mkdir(parents=True, exist_ok=True)

    # Archivos para el visor en vivo: .meta (modelo, tarea), .events (eventos JSON) y .done (estado final).
    view_dir = LOGS_DIR / "antigravity"
    view_dir.mkdir(parents=True, exist_ok=True)
    base = view_dir / f"{_stamp()}-{_slug(instruccion)}"
    events_file, done_file = base.with_suffix(".events"), base.with_suffix(".done")
    events_file.write_text("", encoding="utf-8")

    if ANTIGRAVITY_MODE == "api":
        principal = (modelo_antigravity or ANTIGRAVITY_MODEL).strip()
    else:
        principal = (modelo_antigravity or ANTIGRAVITY_AGY_MODEL).strip()
        if SHOW_CONSOLE and ANTIGRAVITY_VISTA == "tui":
            res, final = await _personal_agy_tui(instruccion, contexto_adicional, target, folder, principal,
                                                 conversacion)
            return _cierre_personal(res, pedido_id, item, final, folder)
    base.with_suffix(".meta").write_text(f"{principal}\n{instruccion[:300].replace(chr(10), ' ')}\n", encoding="utf-8")
    _open_console(f"Antigravity · {principal}",
                  [sys.executable, str(BASE_DIR / "visor_antigravity.py"), str(events_file)], BASE_DIR)

    if ANTIGRAVITY_MODE == "api":
        res = await _personal_api(instruccion, contexto_adicional, target, principal, events_file, done_file)
    else:
        res = await _personal_agy(instruccion, contexto_adicional, target, folder, principal, events_file, done_file,
                                  conversacion)
    return _cierre_personal(res, pedido_id, item, principal, folder)


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


def _emit(events_file: Path, event: dict) -> None:
    with open(events_file, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, ensure_ascii=False) + "\n")


AGY_SETTINGS = Path.home() / ".gemini" / "antigravity-cli" / "settings.json"


def _agy_confiar(folder: Path) -> bool:
    """Marca la carpeta como confiable en agy para que no pida confirmarla. Solo carpetas personales o de
    universidad: la ruta ya pasó por _inside antes de llegar aquí, pero se vuelve a comprobar."""
    if not _inside(folder, PERSONAL_DIR, UNIVERSIDAD_DIR):
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


_AGY_TASKS: dict[str, dict] = {}  # tareas preparadas para la terminal interna de Claude (por task_id)


_ascii = plataforma.safe_prompt  # prompt apto para el panel de terminal de Claude (según el shell)


def _agy_prepare(instruccion: str, contexto: str, target: Path, folder: Path, model: str) -> dict:
    task_id = f"{_stamp()}-{_slug(instruccion, 24)}"
    result_file = folder / f".resultado-{task_id}.md"
    destino = f" Guarda el resultado principal en {target}." if target.suffix else ""
    # La instrucción va directa en el prompt (leer un archivo de tareas le exigía aprobar un comando).
    prompt = (
        f"[Tarea delegada por Claude] {instruccion}"
        + (f" Contexto adicional: {contexto}" if contexto else "") + destino +
        f" REGLAS: trabaja solo dentro de {folder}. Trabaja creando y editando archivos con tus herramientas de "
        "archivos y evita comandos de terminal (solo si es imprescindible, pídelo y el usuario lo aprobará). "
        "No pegues el código completo en el chat. No instales paquetes globales. "
        # El panel de terminal obliga a mandar el prompt sin tildes: se pide explícitamente buena ortografía.
        "ORTOGRAFIA: estas instrucciones te llegan sin tildes por limitaciones tecnicas, pero todo lo que escribas "
        "en espanol (documentos, textos de interfaz, comentarios) debe llevar ortografia correcta: tildes, la letra "
        "ene con virgulilla y signos de apertura de pregunta y exclamacion. "
        f"AL TERMINAR, como último paso obligatorio, crea el archivo {result_file} con una sección "
        "'## RESUMEN' de máximo 8 viñetas: lo realizado, los archivos creados o modificados y cómo probarlo."
    )
    task = {"id": task_id, "folder": folder, "result": result_file, "model": model, "instruccion": instruccion,
            "contexto": contexto, "target": target, "prompt": prompt, "before": _snapshot(folder),
            "inicio": datetime.now(timezone.utc)}
    _AGY_TASKS[task_id] = task
    return task


async def _agy_wait(task: dict, via: str) -> str:
    folder, result_file, model = task["folder"], task["result"], task["model"]
    deadline, last_size, vuelta = time.monotonic() + ANTIGRAVITY_TIMEOUT, -1, 0
    while time.monotonic() < deadline:
        await asyncio.sleep(2)
        if result_file.exists():
            size = result_file.stat().st_size
            if size and size == last_size:  # espera a que termine de escribirse
                break
            last_size = size
            continue
        vuelta += 1
        if vuelta % 5 == 0:  # cada ~10 s revisa si agy quedó atascado por el límite del modelo o sin red
            tipo, msg = _agy_fallo(_agy_conversacion(folder), task["inicio"])
            if tipo:
                return _agy_corte(task, tipo, msg)
    else:
        _live("■ Timeout esperando el resultado de Antigravity")
        return (f"Sin resultado: Antigravity no escribió {result_file.name} en {ANTIGRAVITY_TIMEOUT}s. Su terminal "
                f"sigue abierta; revisa si espera una aprobación o si sigue trabajando. task_id: {task['id']}")

    content = result_file.read_text(encoding="utf-8", errors="replace").strip()
    result_file.unlink(missing_ok=True)
    _AGY_TASKS.pop(task["id"], None)
    after = _snapshot(folder)
    changed = sorted(p for p, m in after.items() if task["before"].get(p) != m and Path(p).name != result_file.name)

    log_dir = LOGS_DIR / "personal"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"{task['id']}.md"
    log_file.write_text(f"# Antigravity ({model}, {via})\n\n- carpeta: {folder}\n\n## Tarea\n\n{task['instruccion']}\n\n"
                        "## Archivos\n\n" + "\n".join(f"- {p}" for p in changed) + f"\n\n## Resultado\n\n{content}\n",
                        encoding="utf-8")
    m = re.search(r"##\s*RESUMEN\s*(.+)$", content, flags=re.S | re.I)
    resumen = m.group(1).strip() if m else _tail(content, 800)
    shown = "\n".join(f"  - {p}" for p in changed[:15]) + (f"\n  … y {len(changed) - 15} más" if len(changed) > 15 else "")
    conv = _agy_conversacion(folder)
    tokens = _agy_tokens(conv, task["inicio"]) if conv else {}
    tok_txt = (f"Tokens: {tokens['input'] + tokens['cache_read']:,} entrada ({tokens['cache_read']:,} de caché) "
               f"/ {tokens['output']:,} salida\n".replace(",", ".") if tokens else "")
    _live(f"■ Éxito con {model} ({via}) | {len(changed)} archivos | {tok_txt.strip()} | síntesis devuelta a Claude")
    return (f"Éxito: Antigravity ({model}, CLI oficial) completó la tarea en {folder}.\n"
            f"Archivos creados/modificados: {len(changed)}\n{shown}\n{tok_txt}Log: {log_file}\n"
            + (f"conversacion_antigravity: {conv} (pásala en una ronda de corrección para conservar el contexto)\n"
               if conv else "")
            + f"La terminal de Antigravity sigue abierta por si quieres continuar la conversación ahí.\n\n"
            f"Síntesis:\n{_tail(resumen)}")


def _agy_corte(task: dict, tipo: str, msg: str) -> str:
    """Resultado cuando agy se atasca: por límite del modelo (propone el siguiente de la cadena) o sin red."""
    _AGY_TASKS.pop(task["id"], None)
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


async def _personal_agy_tui(instruccion: str, contexto: str, target: Path, folder: Path, model: str,
                            conversacion: str = "") -> tuple[str, str]:
    """Ejecuta la tarea en la TUI oficial de Antigravity (`agy -i`) en una ventana de terminal externa.
    Si el modelo llega a su límite, relanza solo con el siguiente de ANTIGRAVITY_CADENA. Devuelve (resultado, modelo)."""
    agy = _agy_cmd()
    if not agy:
        return f"Error: no se encontró agy en '{ANTIGRAVITY_CLI_PATH}'. Revisa ANTIGRAVITY_CLI_PATH en .env.", model
    descartados: list[str] = []
    avisos = ""
    while True:
        _agy_confiar(folder)
        task = _agy_prepare(instruccion, contexto, target, folder, model)
        task["descartados"] = descartados
        prompt = " ".join(task["prompt"].split())
        if plataforma.IS_WINDOWS:
            # Windows Terminal separa comandos con ';' y reinterpreta comillas: se neutralizan en el prompt.
            prompt = prompt.replace(";", ",").replace('"', "'")
        _live(f"  modelo: {model} | TUI oficial de agy en {folder}")
        conv = ["--conversation", conversacion] if _conv_valida(conversacion) else []
        _open_console(f"Antigravity · {model}",
                      [agy, *conv, "-i", prompt, "--model", model, "--mode", "accept-edits"], folder)
        res = await _agy_wait(task, "ventana externa")
        sig = re.search(r"siguiente_modelo: (\S+)", res)
        if not res.startswith("Límite") or not sig or sig.group(1) == "ninguno":
            return res + ("\n" + avisos if avisos else ""), model
        # Límite del modelo: relanza con el siguiente de la cadena y la misma conversación (conserva el contexto).
        _cierre_personal(res, "", 0, model, folder)  # registra lo gastado por el modelo que se agotó
        descartados.append(model)
        avisos += f"⚠ {model} llegó a su límite; se relanzó con {sig.group(1)} (cierra la ventana anterior).\n"
        conv_m = re.search(r"conversacion_antigravity: (\S+)", res)
        conversacion = conv_m.group(1) if conv_m else conversacion
        model = sig.group(1)


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
    """SOLO EN CLAUDE CODE (terminal interna). Paso 1 de 2 para una tarea PERSONAL en Antigravity.
    Para trabajar en paralelo, prepara varias tareas (carpetas distintas) y abre una terminal por cada una.
    conversacion: en una RONDA DE CORRECCIÓN, la conversacion_antigravity del resultado anterior (misma carpeta):
        Antigravity continúa con todo el contexto y solo necesita la instrucción de corrección.
    modelos_descartados: al relanzar tras un 'Límite:', los modelos_descartados que devolvió (separados por coma),
        para que un nuevo límite proponga un modelo que no se haya agotado ya.

    Mismos parámetros y reglas de modelo que ejecutar_personal_antigravity. Devuelve un task_id y un comando:
    ejecútalo tal cual con run_in_terminal (título 'Antigravity', sin cwd: el comando ya entra a la carpeta).
    La primera vez en una carpeta, agy pide confiar en ella: avisa al usuario que confirme solo si la ruta
    mostrada es la carpeta personal. Luego llama esperar_resultado_antigravity(task_id) para la síntesis.
    """
    target = _resolve_personal(ruta_salida_opcional or "_antigravity")
    if not _inside(target, PERSONAL_DIR, UNIVERSIDAD_DIR):
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
    task = _agy_prepare(instruccion, contexto_adicional, target, folder, model)
    task.update(pedido_id=pedido_id, item=item,
                descartados=[m.strip() for m in modelos_descartados.split(",") if m.strip()])
    if pedido_id and item:
        registro.actualizar_item(pedido_id, item, "en_curso", agente="Antigravity", modelo=model)
    _live("")
    _live(f"▶ PERSONAL → Antigravity (terminal de Claude) | {folder} | {model}")
    # Entra primero a la carpeta personal: agy toma el directorio actual como espacio de trabajo y pide
    # confiar en él (sin esto pediría confiar en la carpeta de la sesión de Claude, p. ej. la de trabajo).
    conv = ["--conversation", conversacion] if _conv_valida(conversacion) else []
    cmd = plataforma.run_line(agy, *conv, "-i", plataforma.quote_prompt(_ascii(task["prompt"])), "--model", model,
                              "--mode", "accept-edits", cwd=folder)
    return (f"task_id: {task['id']}\ncarpeta: {folder}{aviso_item}\n"
            f"comando (ejecútalo con run_in_terminal, una sola línea, sin cambios; luego muestra la pestaña con show_pane):\n{cmd}")


@mcp.tool()
async def esperar_resultado_antigravity(task_id: str) -> str:
    """SOLO EN CLAUDE CODE. Paso 2 de 2: espera a que Antigravity termine la tarea preparada y devuelve
    la síntesis (archivos creados/modificados y resumen)."""
    task = _AGY_TASKS.get(task_id)
    if not task:
        return f"Error: no hay una tarea preparada con task_id '{task_id}' en este servidor."
    res = await _agy_wait(task, "terminal de Claude")
    out = _cierre_personal(res, task.get("pedido_id", ""), task.get("item", 0), task["model"], task["folder"])
    if res.startswith("Límite") and "siguiente_modelo: ninguno" not in res:
        out += ("\nQué hacer: cierra la pestaña de Antigravity (stop_terminal_tab) y relanza el mismo ítem con "
                "preparar_tarea_antigravity(misma instrucción, modelo_antigravity=<siguiente_modelo>, "
                "conversacion=<conversacion_antigravity>, modelos_descartados=<modelos_descartados>, pedido_id, item). "
                "Avisa al usuario en una línea del cambio de modelo.")
    return out


def _step_label(info: dict) -> str:
    params = (info or {}).get("parameters") or {}
    for key in ("TargetFile", "AbsolutePath", "CommandLine", "Query", "SearchPath", "Url", "DirectoryPath"):
        if params.get(key):
            return str(params[key])
    return next((str(v) for v in params.values() if isinstance(v, (str, int))), "")


async def _personal_agy(instruccion: str, contexto: str, target: Path, folder: Path, principal: str,
                        events_file: Path, done_file: Path, conversacion: str = "") -> str:
    agy = _agy_cmd()
    if not agy:
        done_file.write_text("✖ No se encontró el CLI de Antigravity (agy)", encoding="utf-8")
        return f"Error: no se encontró agy en '{ANTIGRAVITY_CLI_PATH}'. Revisa ANTIGRAVITY_CLI_PATH en .env."

    prompt = instruccion
    if contexto:
        prompt += f"\n\nContexto adicional:\n{contexto}"
    if target.suffix:
        prompt += f"\n\nGuarda el resultado principal en: {target}"
    prompt += (f"\n\nTrabaja solo dentro de {folder}. Escribe los artefactos (código, documentos) en archivos; no "
               "los pegues completos en la respuesta. No instales paquetes globales ni cambies la configuración del "
               "sistema.")
    if not ANTIGRAVITY_SKIP_PERMISSIONS:
        prompt += (" No ejecutes comandos de terminal (no tienes permiso para hacerlo): trabaja únicamente creando "
                   "y editando archivos, y si algo debería probarse, indica en el resumen cómo hacerlo.")
    prompt += (" Termina tu respuesta con una sección '## RESUMEN' de máximo 8 viñetas con lo realizado, los "
               "archivos creados y los hallazgos clave.")

    cadena = ANTIGRAVITY_CADENA[ANTIGRAVITY_CADENA.index(principal) + 1:] if principal in ANTIGRAVITY_CADENA else []
    modelos = list(dict.fromkeys([principal, *cadena, ANTIGRAVITY_AGY_MODEL, *ANTIGRAVITY_AGY_FALLBACK]))
    before = _snapshot(folder)
    fallos: list[str] = []
    for model in modelos:
        _live(f"  modelo: {model} | agy en {folder}")
        if model != modelos[0]:
            _emit(events_file, {"event": "restart", "model": model})
        cmd = [agy, "-p", prompt, "--model", model,
               "--output-format", "stream-json", "--print-timeout", f"{ANTIGRAVITY_TIMEOUT}s"]
        if _conv_valida(conversacion):
            cmd += ["--conversation", conversacion]
        # Por defecto solo edita archivos. Con permisos saltados también ejecuta comandos (terminal en sandbox,
        # pero sus herramientas de archivos pueden escribir fuera de la carpeta: por eso es opcional).
        cmd += ["--dangerously-skip-permissions", "--sandbox"] if ANTIGRAVITY_SKIP_PERMISSIONS else ["--mode", "accept-edits"]
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=str(folder), stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        response, result, otros = [], None, []
        assert proc.stdout
        try:
            async def pump() -> None:
                nonlocal result
                async for raw in proc.stdout:
                    line = raw.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    try:
                        ev = json.loads(line)
                    except ValueError:
                        otros.append(line)
                        _live(f"  │ {line}")
                        continue
                    with open(events_file, "a", encoding="utf-8") as fh:
                        fh.write(line + "\n")
                    if ev.get("event") == "result":
                        result = ev.get("result") or {}
                        continue
                    st = ev.get("step_update") or {}
                    if st.get("text_delta"):
                        response.append(st["text_delta"])
                    if st.get("step_type") == "tool" and st.get("state") == "ACTIVE":
                        _live(f"  ⚙ {st.get('tool_name')} {_step_label(st.get('tool_info'))}")

            await asyncio.wait_for(pump(), timeout=ANTIGRAVITY_TIMEOUT + 30)
            await proc.wait()
        except asyncio.TimeoutError:
            proc.kill()
            fallos.append(f"{model}: timeout")
            continue

        status = (result or {}).get("status", "")
        if status == "SUCCESS":
            break
        motivo = status or _tail(" ".join(otros), 200) or f"exit {proc.returncode}"
        fallos.append(f"{model}: {motivo}")
        _live(f"  ✖ {model}: {motivo}")
    else:
        done_file.write_text("✖ Antigravity no completó la tarea: " + "; ".join(fallos), encoding="utf-8")
        _live("■ Error: Antigravity no completó la tarea")
        return "Error: Antigravity no completó la tarea. " + "; ".join(fallos)

    content = (result.get("response") or "".join(response)).strip()
    usage = result.get("usage") or {}
    after = _snapshot(folder)
    changed = sorted(p for p, m in after.items() if before.get(p) != m)

    log_dir = LOGS_DIR / "personal"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"{_stamp()}-{_slug(instruccion)}.md"
    log_file.write_text(
        f"# Antigravity ({model})\n\n- carpeta: {folder}\n- conversación: {result.get('conversation_id')}\n"
        f"- tokens: {usage}\n\n## Prompt\n\n{prompt}\n\n## Archivos\n\n" + "\n".join(f"- {p}" for p in changed) +
        f"\n\n## Respuesta\n\n{content}\n", encoding="utf-8")

    m = re.search(r"##\s*RESUMEN\s*(.+)$", content, flags=re.S | re.I)
    resumen = m.group(1).strip() if m else _tail(content, 800)
    tokens = f"{usage.get('input_tokens', 0):,} entrada / {usage.get('output_tokens', 0):,} salida".replace(",", ".")
    denied = [d.get("display_name") or d.get("action") for d in result.get("denied_actions") or []]
    if not content and not changed:
        motivo = f"se le negaron: {', '.join(denied)}" if denied else "terminó sin respuesta ni archivos"
        _live(f"■ Incompleto: {motivo}")
        done_file.write_text(f"✖ Antigravity se detuvo: {motivo}", encoding="utf-8")
        return (f"Incompleto: Antigravity ({model}) se detuvo sin crear archivos ({motivo}). "
                f"Log: {log_file}. Reformula la tarea para que solo cree/edite archivos, o activa "
                "ANTIGRAVITY_SKIP_PERMISSIONS=1 si necesita ejecutar comandos.")
    if denied:
        resumen = f"(Acciones denegadas: {', '.join(denied)})\n{resumen}"
    aviso = f" (se pidió {principal}; antes fallaron: {', '.join(fallos)})" if fallos else ""
    shown = "\n".join(f"  - {p}" for p in changed[:15]) + (f"\n  … y {len(changed) - 15} más" if len(changed) > 15 else "")
    _live(f"■ Éxito con {model} | {len(changed)} archivos | tokens {tokens} | síntesis devuelta a Claude")
    done_file.write_text(f"✓ {model} · {len(changed)} archivos en {folder} · síntesis enviada a Claude", encoding="utf-8")
    return (f"Éxito: Antigravity ({model}{aviso}) completó la tarea en {folder}.\n"
            f"Archivos creados/modificados: {len(changed)} | Tokens: {tokens}\n{shown}\n"
            + (f"conversacion_antigravity: {result.get('conversation_id')} (pásala en una ronda de corrección)\n"
               if result.get("conversation_id") else "")
            + f"Log completo: {log_file}\n\nSíntesis:\n{_tail(resumen)}")


async def _personal_api(instruccion: str, contexto: str, target: Path, principal: str,
                        events_file: Path, done_file: Path) -> str:
    """Respaldo sin agente: llama a la API de Gemini con ANTIGRAVITY_API_KEY y guarda la respuesta."""
    if not ANTIGRAVITY_API_KEY:
        done_file.write_text("✖ Falta ANTIGRAVITY_API_KEY", encoding="utf-8")
        return "Error: falta ANTIGRAVITY_API_KEY en .env (o usa ANTIGRAVITY_MODE=agy)."

    system = (
        "Eres el ejecutor de tareas personales. Entrega el resultado completo (código, documento o "
        "análisis) en Markdown. Termina SIEMPRE con una sección '## RESUMEN' de máximo 8 viñetas "
        "con lo realizado y los hallazgos clave."
    )
    user = instruccion + (f"\n\n### Contexto adicional\n{contexto}" if contexto else "")
    content, used_model, fallos = None, "", []
    modelos = list(dict.fromkeys([principal, ANTIGRAVITY_MODEL, *ANTIGRAVITY_FALLBACK_MODELS]))
    async with httpx.AsyncClient(timeout=ANTIGRAVITY_TIMEOUT) as c:
        # Dos rondas: la saturación (503) de Google suele ser momentánea.
        for intento, model in enumerate(modelos * 2):
            if intento == len(modelos):
                await asyncio.sleep(10)
            _live(f"  probando {model}…")
            try:
                async with c.stream(
                    "POST", f"{ANTIGRAVITY_BASE_URL}/chat/completions",
                    headers={"Authorization": f"Bearer {ANTIGRAVITY_API_KEY}"},
                    json={"model": model, "stream": True,
                          "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]},
                ) as r:
                    if r.status_code in (404, 429) or r.status_code >= 500:
                        fallos.append(f"{model}: HTTP {r.status_code}")
                        continue
                    if r.status_code >= 400:
                        body = (await r.aread()).decode("utf-8", errors="replace")
                        return f"Error HTTP {r.status_code} de Gemini ({model}): {_tail(body, 400)}"
                    _emit(events_file, {"event": "restart", "model": model})
                    parts = []
                    async for raw in r.aiter_lines():
                        if not raw.startswith("data:") or raw.strip() == "data: [DONE]":
                            continue
                        try:
                            delta = json.loads(raw[5:])["choices"][0]["delta"].get("content") or ""
                        except (ValueError, KeyError, IndexError):
                            continue
                        parts.append(delta)
                        _emit(events_file, {"event": "step_update", "step_update": {
                            "step_type": "agent_response", "state": "ACTIVE", "text_delta": delta}})
            except Exception as e:  # noqa: BLE001
                fallos.append(f"{model}: {e}")
                continue
            content, used_model = "".join(parts), model
            break
    if content is None:
        done_file.write_text("✖ Ningún modelo de Gemini respondió: " + "; ".join(fallos), encoding="utf-8")
        return "Error: ningún modelo de Gemini respondió. " + "; ".join(fallos)

    out_file = target if target.suffix else target / f"{_stamp()}-{_slug(instruccion)}.md"
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text(content, encoding="utf-8")
    m = re.search(r"##\s*RESUMEN\s*(.+)$", content, flags=re.S | re.I)
    resumen = m.group(1).strip() if m else _tail(content, 800)
    aviso = f"; antes fallaron: {', '.join(fallos)}" if fallos else ""
    done_file.write_text(f"✓ {used_model} · guardado en {out_file} · síntesis enviada a Claude", encoding="utf-8")
    return (f"Éxito: Gemini API ({used_model}{aviso}) completó la tarea. "
            f"Resultado completo: {out_file} ({content.count(chr(10)) + 1} líneas)\n\n"
            f"Síntesis:\n{_tail(resumen)}")


# ----------------------------------------------------------------------------- configuración

@mcp.tool()
def ver_configuracion() -> str:
    """Carpetas de enrutamiento, modelos por defecto y sistema operativo de esta instalación.
    Úsala al empezar una sesión para saber qué ruta es trabajo y cuál personal en esta máquina."""
    sistema = "Windows" if plataforma.IS_WINDOWS else "macOS" if plataforma.IS_MAC else "Linux"
    return (f"Sistema: {sistema} (comandos de terminal en {'PowerShell' if plataforma.IS_WINDOWS else 'zsh/bash'})\n"
            f"TRABAJO (OpenCode): {TRABAJO_DIR} · modelo por defecto {INNTI_PROVIDER}/{INNTI_DEFAULT_MODEL}\n"
            f"PERSONAL (Antigravity): {PERSONAL_DIR} · UNIVERSIDAD: {UNIVERSIDAD_DIR} · "
            f"modo {ANTIGRAVITY_MODE}/{ANTIGRAVITY_VISTA} · modelo {ANTIGRAVITY_AGY_MODEL}\n"
            f"Checklists: {registro.TAREAS_DIR} · logs: {LOGS_DIR} · panel: {'configurado' if PANEL_URL else 'sin PANEL_URL'}\n"
            f"Rondas de corrección máximas: {registro.MAX_RONDAS}")


# ----------------------------------------------------------------------------- verificación, aislamiento y presupuesto

@mcp.tool()
async def verificar_proyecto(ruta: str, comandos: list[str] | None = None) -> str:
    """Corre los tests/lint del proyecto que contiene `ruta` y devuelve VERDE/ROJO con el final de cada salida.
    Usa los comandos guardados con configurar_verificacion o los detecta (Maven, Gradle, npm, pytest, Go).
    Úsala en cada revisión de una delegación de trabajo (también dentro del worktree aislado)."""
    target = _resolve_under(TRABAJO_DIR, ruta)
    if not _inside(target, TRABAJO_DIR, PERSONAL_DIR, UNIVERSIDAD_DIR):
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


@mcp.tool()
def integrar_trabajo(worktree: str, accion: str = "aplicar") -> str:
    """Cierra un trabajo aislado. accion: 'aplicar' (squash al repo original: cambios en staging SIN commit),
    'descartar' (borra worktree y rama, el repo no cambia) o 'conservar'."""
    return control.integrar(worktree, accion)


@mcp.tool()
def listar_trabajos_aislados() -> str:
    """Worktrees del orquestador pendientes de aplicar o descartar."""
    return control.listar_worktrees()


@mcp.tool()
def estado_presupuesto() -> str:
    """Gasto de Innti de hoy y del mes frente a PRESUPUESTO_DIA_USD / PRESUPUESTO_MES_USD."""
    return control.estado_presupuesto()[1]


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
    writes = [{"op": "set", "collection": "estado", "doc_id": doc_id, "file_path": str(path)}]
    if prev and prev != doc_id:
        writes.append({"op": "delete", "collection": "estado", "doc_id": prev, "if_version": 1})
    url = PANEL_URL or "<PANEL_URL sin configurar en .env>"
    return (f"Ejecuta ArtifactData action='batch' url='{url}' writes={json.dumps(writes, ensure_ascii=False)}\n"
            f"({len(data['pedidos'])} pedidos · si el delete falla por versión, repite solo el set.)")


if __name__ == "__main__":
    mcp.run()
