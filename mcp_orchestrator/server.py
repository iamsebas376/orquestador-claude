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
# Todo lo que no es trabajo va a Antigravity: además de personal y universidad, otras carpetas propias
# (por defecto la del propio orquestador). Separadas por coma en OTRAS_CARPETAS_PERSONALES.
OTRAS_CARPETAS = [Path(c.strip()).expanduser() for c in
                  _env("OTRAS_CARPETAS_PERSONALES", str(plataforma.REPO_ROOT)).split(",") if c.strip()]
LOGS_DIR = Path(_env("LOGS_DIR") or plataforma.REPO_ROOT / "logs").expanduser()

OPENCODE_CLI_PATH = _env("OPENCODE_CLI_PATH", "opencode")
OPENCODE_CONFIG_PATH = Path(_env("OPENCODE_CONFIG_PATH") or plataforma.default_opencode_config()).expanduser()
OPENCODE_TIMEOUT = int(_env("OPENCODE_TIMEOUT_SECONDS", "1800"))
# Segundos sin una sola línea de salida tras los que se corta la tarea (modelo colgado o sin red).
OPENCODE_IDLE = int(_env("OPENCODE_IDLE_SECONDS", "600"))
# Servidor OpenCode persistente: las tareas corren en él (sin ventanas) y las sesiones permiten rondas de corrección.
OPENCODE_SERVER_URL = _env("OPENCODE_SERVER_URL", "http://127.0.0.1:4096").rstrip("/")
INNTI_PROVIDER = _env("INNTI_PROVIDER", "litellm-proxy")
INNTI_DEFAULT_MODEL = _env("INNTI_DEFAULT_MODEL", "innti-dsp")
INNTI_BASE_URL = _env("INNTI_BASE_URL").rstrip("/")
INNTI_API_KEY = _env("INNTI_API_KEY")

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
ANTIGRAVITY_TIMEOUT = int(_env("ANTIGRAVITY_TIMEOUT_SECONDS", "900"))
ANTIGRAVITY_IDLE = int(_env("ANTIGRAVITY_IDLE_SECONDS", "600"))  # agy -p no imprime hasta el final

MAX_RETURN_CHARS = int(_env("MAX_RETURN_CHARS", "1500"))
PANEL_URL = _env("PANEL_URL")
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "dist", "build", "target", ".next", "__pycache__", ".angular",
             ".pytest_cache", ".mypy_cache", ".ruff_cache", ".gradle", ".idea", ".vscode"}

mcp = FastMCP("orchestrator")


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


async def _run(cmd: list[str], cwd: Path, timeout: int, idle: int = 0) -> tuple[int, str]:
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

    async def pump() -> None:
        nonlocal inactivo
        assert proc.stdout
        while True:
            try:
                raw = await asyncio.wait_for(proc.stdout.readline(), timeout=idle or None)
            except asyncio.TimeoutError:
                inactivo = True
                return
            if not raw:
                return
            line = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", raw.decode("utf-8", errors="replace")).rstrip()
            lines.append(line)
            if line:
                _live(f"  │ {line}")

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


@mcp.tool()
async def ejecutar_trabajo_opencode_innti(
    prompt_tarea: str,
    modelo_innti: str = "",
    ruta_proyecto_o_archivo: str = "",
    sesion_opencode: str = "",
    pedido_id: str = "",
    item: int = 0,
    forzar_presupuesto: bool = False,
) -> str:
    """CONTEXTO TRABAJO (Innti/corporativo). Delega la tarea a OpenCode con un modelo Innti.
    OpenCode edita los archivos directamente; aquí solo vuelve un resumen de estado, tokens y costo.

    forzar_presupuesto: solo si el usuario autorizó pasar el límite de presupuesto.

    sesion_opencode: session_id de una delegación anterior: reutilizarla en una RONDA DE CORRECCIÓN
        conserva el contexto del agente y cuesta menos. Vacío = sesión nueva. Sin ventanas ni terminales.
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
        # Ronda de corrección: misma sesión.
        session_id = sesion_opencode
        cmd += ["--attach", OPENCODE_SERVER_URL, "--dir", str(cwd), "--session", session_id]
    elif await _ensure_opencode_server(cli) and (session_id := await _create_session(cwd, titulo) or ""):
        # Sesión nueva en el servidor: su id permite una ronda de corrección con el mismo contexto.
        cmd += ["--attach", OPENCODE_SERVER_URL, "--dir", str(cwd), "--session", session_id]
    else:
        _live("  servidor OpenCode no disponible: se ejecuta sin sesión persistente")
    checkpoint = _git_checkpoint(cwd)
    before = _snapshot(cwd)
    inicio_ms = int(time.time() * 1000)
    code, output = await _run([*cmd, prompt], cwd, OPENCODE_TIMEOUT, idle=OPENCODE_IDLE)
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


def _agy_prompt(instruccion: str, contexto: str, target: Path, folder: Path) -> str:
    destino = f" Guarda el resultado principal en {target}." if target.suffix else ""
    return (
        f"[Tarea delegada por Claude] {instruccion}"
        + (f" Contexto adicional: {contexto}" if contexto else "") + destino +
        f" REGLAS: trabaja solo dentro de {folder}. Crea y edita archivos con tus herramientas de archivos; para "
        "leer, listar o buscar usa también tus herramientas de archivos. Los comandos de terminal se deniegan en este "
        "modo: no los uses. No pegues el código completo en el chat. No instales paquetes globales. Escribe en "
        "español con ortografía correcta (tildes, ñ, ¿ y ¡). Al terminar responde SOLO con una sección "
        "'## RESUMEN' de máximo 8 viñetas: lo realizado, los archivos creados o modificados y cómo probarlo."
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


async def _agy_una_vez(agy: str, instruccion: str, contexto: str, target: Path, folder: Path, model: str,
                       conversacion: str, descartados: list[str]) -> str:
    """Una ejecución de `agy -p` (sin ventana ni terminal). Devuelve el texto de resultado para _cierre_personal:
    'Éxito: …', 'Límite: …', 'Sin conexión: …' o 'Error: …'."""
    _agy_confiar(folder)
    task_id = f"{_stamp()}-{_slug(instruccion, 24)}-{os.urandom(2).hex()}"
    prompt = _agy_prompt(instruccion, contexto, target, folder)
    inicio, before = datetime.now(timezone.utc), _snapshot(folder)
    conv = ["--conversation", conversacion] if _conv_valida(conversacion) else []
    cmd = [agy, *conv, "-p", prompt, "--model", model, "--mode", "accept-edits",
           "--print-timeout", f"{ANTIGRAVITY_TIMEOUT}s"]
    _live(f"  modelo: {model} | agy -p en {folder}")
    code, output = await _run(cmd, folder, ANTIGRAVITY_TIMEOUT + 30, idle=ANTIGRAVITY_IDLE)
    task = {"id": task_id, "folder": folder, "model": model, "inicio": inicio, "descartados": descartados}
    tipo, msg = _agy_fallo(_agy_conversacion(folder), inicio)
    if not tipo and code != 0 and _RE_LIMITE.search(output):
        tipo, msg = "limite", output.strip()[-300:]
    if not tipo and _RE_LIMITE.search(output) and "RESUMEN" not in output.upper():
        tipo, msg = "limite", output.strip()[-300:]
    if tipo:
        return _agy_corte(task, tipo, msg)
    if code != 0 or not output.strip():
        return f"Error (exit {code}): Antigravity no devolvió resultado.\n{_tail(output, 600)}\ntask_id: {task_id}"

    after = _snapshot(folder)
    changed = sorted(p for p, m in after.items() if before.get(p) != m)
    log_dir = LOGS_DIR / "personal"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"{task_id}.md"
    log_file.write_text(f"# Antigravity ({model})\n\n- carpeta: {folder}\n\n## Tarea\n\n{instruccion}\n\n"
                        "## Archivos\n\n" + "\n".join(f"- {p}" for p in changed) + f"\n\n## Resultado\n\n{output}\n",
                        encoding="utf-8")
    m = re.search(r"##\s*RESUMEN\s*(.+)$", output, flags=re.S | re.I)
    resumen = m.group(1).strip() if m else _tail(output, 800)
    shown = "\n".join(f"  - {p}" for p in changed[:15]) + (f"\n  … y {len(changed) - 15} más" if len(changed) > 15 else "")
    conv_id = _agy_conversacion(folder)
    tokens = _agy_tokens(conv_id, inicio) if conv_id else {}
    tok_txt = (f"Tokens: {tokens['input'] + tokens['cache_read']:,} entrada ({tokens['cache_read']:,} de caché) "
               f"/ {tokens['output']:,} salida\n".replace(",", ".") if tokens else "")
    _live(f"■ Éxito con {model} | {len(changed)} archivos | {tok_txt.strip()} | síntesis devuelta a Claude")
    return (f"Éxito: Antigravity ({model}, CLI oficial) completó la tarea en {folder}.\n"
            f"Archivos creados/modificados: {len(changed)}\n{shown}\n{tok_txt}Log: {log_file}\n"
            + (f"conversacion_antigravity: {conv_id} (pásala en una ronda de corrección para conservar el contexto)\n"
               if conv_id else "")
            + f"\nSíntesis:\n{_tail(resumen)}")


async def _personal_agy(instruccion: str, contexto: str, target: Path, folder: Path, model: str,
                        conversacion: str = "", descartados: list[str] | None = None) -> tuple[str, str]:
    """Ejecuta la tarea con `agy -p`. Si el modelo llega a su límite, relanza solo con el siguiente de
    ANTIGRAVITY_CADENA (misma conversación). Devuelve (resultado, modelo usado)."""
    agy = _agy_cmd()
    if not agy:
        return f"Error: no se encontró agy en '{ANTIGRAVITY_CLI_PATH}'. Revisa ANTIGRAVITY_CLI_PATH en .env.", model
    descartados = list(descartados or [])
    avisos = ""
    while True:
        res = await _agy_una_vez(agy, instruccion, contexto, target, folder, model, conversacion, descartados)
        sig = re.search(r"siguiente_modelo: (\S+)", res)
        if not res.startswith("Límite") or not sig or sig.group(1) == "ninguno":
            return res + ("\n" + avisos if avisos else ""), model
        _cierre_personal(res, "", 0, model, folder)  # registra lo gastado por el modelo que se agotó
        descartados.append(model)
        avisos += f"⚠ {model} llegó a su límite; se relanzó con {sig.group(1)}.\n"
        conv_m = re.search(r"conversacion_antigravity: (\S+)", res)
        conversacion = conv_m.group(1) if conv_m else conversacion
        model = sig.group(1)


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
    """CONTEXTO PERSONAL (todo lo que no es D:\\workspaces). Delega la tarea a Antigravity (`agy -p`, plan Pro)
    sin abrir ventanas ni terminales: espera a que termine y devuelve la síntesis. Edita archivos; los comandos
    de terminal se deniegan (verifica tú con verificar_proyecto). Varias delegaciones en el mismo mensaje corren
    en paralelo (que no compartan archivos). Si el modelo llega a su límite se reintenta solo con el siguiente.

    conversacion: en una ronda de corrección, la conversacion_antigravity del resultado anterior.
    pedido_id, item: vinculan la delegación al checklist (el ítem pasa a 'revisando').

    modelo_antigravity: elige según la tarea (vacío = ANTIGRAVITY_AGY_MODEL, gemini-3.8-flash-medium):
        'gemini-3.8-flash-low'    → simple: resúmenes, listas, reformatear, preguntas rápidas.
        'gemini-3.8-flash-medium' → normal: código personal, planes, explicaciones.
        'gemini-4-argon-medium'   → complejo: proyectos con varios archivos, diseño, depuración difícil.
        'gemini-4-argon-high'     → profundo: razonamiento largo o análisis exigente (más lento).
    instruccion: la tarea a realizar (autocontenida).
    contexto_adicional: datos, restricciones o contexto de apoyo.
    ruta_salida_opcional: archivo o carpeta dentro de PERSONAL_DIR, UNIVERSIDAD_DIR u OTRAS_CARPETAS. Relativa:
        'universidad/curso/...' -> UNIVERSIDAD_DIR/curso/...; cualquier otra -> PERSONAL_DIR/...
        Vacía -> PERSONAL_DIR/_antigravity.
    """
    target = _resolve_personal(ruta_salida_opcional or "_antigravity")
    _live("")
    _live(f"▶ PERSONAL → Antigravity | salida: {target}")
    _live(f"  instrucción: {instruccion[:300]}")
    if not _inside(target, PERSONAL_DIR, UNIVERSIDAD_DIR, *OTRAS_CARPETAS):
        _live(f"  ✖ rechazado: ruta fuera de {PERSONAL_DIR} y {UNIVERSIDAD_DIR}")
        return (f"Rechazado: {target} está fuera de {PERSONAL_DIR} y {UNIVERSIDAD_DIR}. Si es un "
                "proyecto de trabajo usa ejecutar_trabajo_opencode_innti.")

    folder = target.parent if target.suffix else target
    folder.mkdir(parents=True, exist_ok=True)
    principal = (modelo_antigravity or ANTIGRAVITY_AGY_MODEL).strip()
    aviso_item = ""
    if not (pedido_id and item):  # ajuste sin pedido: queda como ítem nuevo del pedido del proyecto
        pedido_id, item, aviso_item = registro.item_automatico(str(folder), instruccion)
    if pedido_id and item:
        registro.actualizar_item(pedido_id, item, "en_curso", agente="Antigravity", modelo=principal)
    res, final = await _personal_agy(instruccion, contexto_adicional, target, folder, principal, conversacion)
    return _cierre_personal(res, pedido_id, item, final, folder) + aviso_item



# ----------------------------------------------------------------------------- configuración

@mcp.tool()
def ver_configuracion() -> str:
    """Carpetas de enrutamiento, modelos por defecto y sistema operativo de esta instalación.
    Úsala al empezar una sesión para saber qué ruta es trabajo y cuál personal en esta máquina."""
    return (f"Sistema: Windows (comandos de terminal en PowerShell)\n"
            f"TRABAJO (OpenCode): {TRABAJO_DIR} · modelo por defecto {INNTI_PROVIDER}/{INNTI_DEFAULT_MODEL}\n"
            f"PERSONAL (Antigravity): {PERSONAL_DIR} · UNIVERSIDAD: {UNIVERSIDAD_DIR} · "
            f"modelo {ANTIGRAVITY_AGY_MODEL}\n"
            f"Checklists: {registro.TAREAS_DIR} · logs: {LOGS_DIR} · panel: {'configurado' if PANEL_URL else 'sin PANEL_URL'}\n"
            f"Rondas de corrección máximas: {registro.MAX_RONDAS}")


# ----------------------------------------------------------------------------- verificación y presupuesto

@mcp.tool()
async def verificar_proyecto(ruta: str, comandos: list[str] | None = None) -> str:
    """Corre los tests/lint del proyecto que contiene `ruta` y devuelve VERDE/ROJO con el final de cada salida.
    Usa los comandos guardados con configurar_verificacion o los detecta (Maven, Gradle, npm, pytest, Go).
    Úsala en cada revisión de una delegación de trabajo."""
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
