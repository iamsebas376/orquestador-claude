"""Control de calidad y gasto: verificación por proyecto, trabajo aislado en worktrees y presupuesto de Innti."""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path

import plataforma
import registro

VERIF_FILE = Path(os.getenv("VERIFICACION_FILE") or plataforma.REPO_ROOT / "verificacion.json")
WORKTREES_FILE = registro.LOGS_DIR / "worktrees.json"
VERIF_TIMEOUT = int(os.getenv("VERIFICACION_TIMEOUT_SECONDS") or "900")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _git(args: list[str], cwd: Path, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, timeout=timeout,
                          encoding="utf-8", errors="replace")


# ----------------------------------------------------------------------------- verificación por proyecto

def _leer_verif() -> dict:
    try:
        return json.loads(VERIF_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def guardar_verificacion(proyecto: str, comandos: list[str]) -> str:
    data = _leer_verif()
    if comandos:
        data[proyecto] = comandos
    else:
        data.pop(proyecto, None)
    VERIF_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return (f"Verificación de '{proyecto}' guardada: {' && '.join(comandos)}" if comandos
            else f"Verificación de '{proyecto}' eliminada (se volverá a detectar sola).")


def detectar_comandos(cwd: Path, tope: Path) -> tuple[Path, list[str]]:
    """Busca hacia arriba (hasta `tope`) el primer directorio con archivos de build y propone comandos."""
    d = cwd if cwd.is_dir() else cwd.parent
    tope = tope.resolve()
    while True:
        cmds: list[str] = []
        if (d / "pom.xml").exists():
            wrapper = "mvnw.cmd" if plataforma.IS_WINDOWS else "mvnw"
            prog = (".\\" if plataforma.IS_WINDOWS else "./") + wrapper if (d / wrapper).exists() else "mvn"
            cmds.append(prog + " -q -B test")
        elif any((d / g).exists() for g in ("build.gradle", "build.gradle.kts")):
            wrapper = "gradlew.bat" if plataforma.IS_WINDOWS else "gradlew"
            prog = (".\\" if plataforma.IS_WINDOWS else "./") + wrapper if (d / wrapper).exists() else "gradle"
            cmds.append(prog + " test -q")
        elif (d / "package.json").exists():
            try:
                scripts = json.loads((d / "package.json").read_text(encoding="utf-8")).get("scripts", {})
            except (OSError, ValueError):
                scripts = {}
            if "lint" in scripts:
                cmds.append("npm run lint --silent")
            if "test" in scripts and "no test specified" not in scripts["test"]:
                cmds.append("npm test --silent")
        elif (d / "go.mod").exists():
            cmds.append("go test ./...")
        elif any((d / f).exists() for f in ("pyproject.toml", "pytest.ini", "setup.cfg", "tox.ini")) or \
                any(d.glob("test_*.py")) or (d / "tests").is_dir() and any((d / "tests").glob("test_*.py")):
            cmds.append("python -m pytest -q")
        if cmds or d.resolve() == tope or d.parent == d:
            return d, cmds
        d = d.parent


def comandos_para(ruta: Path, tope: Path) -> tuple[Path, list[str], str]:
    """Comandos configurados (por ruta o por nombre de proyecto) o detectados. Devuelve (carpeta, cmds, origen)."""
    data = _leer_verif()
    # Dentro de un worktree del orquestador, la configuración se busca con la ruta equivalente del repo original.
    info = worktree_info(ruta)
    original = Path(info["repo"]) / ruta.resolve().relative_to(Path(info["worktree"])) if info else ruta
    r = str(original.resolve()).lower()
    for clave, cmds in sorted(data.items(), key=lambda kv: -len(kv[0])):
        if ("\\" in clave or "/" in clave) and r.startswith(str(Path(clave).resolve()).lower()):
            carpeta = ruta if info else Path(clave)
            return carpeta, cmds, "configurados"
    proyecto = registro.proyecto_de(str(original))
    if proyecto in data:
        # Se ejecuta en la raíz del proyecto (o del worktree que lo reemplaza), no en la carpeta de trabajo.
        raiz = tope / proyecto
        carpeta = Path(info["worktree"]) if info else (raiz if raiz.is_dir() else ruta)
        return carpeta, data[proyecto], "configurados"
    carpeta, cmds = detectar_comandos(ruta, tope)
    return carpeta, cmds, "detectados"


async def verificar(ruta: Path, tope: Path, comandos: list[str] | None, logs_dir: Path) -> str:
    carpeta, cmds, origen = (ruta, comandos, "indicados") if comandos else comandos_para(ruta, tope)
    if not cmds:
        return (f"Sin verificación: no encontré archivos de build entre {ruta} y {tope}. "
                "Configúrala con configurar_verificacion(proyecto, comandos).")
    avisos = []
    if (carpeta / "package.json").exists() and not (carpeta / "node_modules").exists():
        avisos.append("⚠ no hay node_modules en esta carpeta (¿worktree nuevo?): ejecuta la instalación de "
                      "dependencias antes, o los comandos de npm fallarán.")
    logs_dir.mkdir(parents=True, exist_ok=True)
    log = logs_dir / f"{datetime.now():%Y%m%d-%H%M%S-%f}-{registro._slug(carpeta.name, 30)}.log"
    lineas, ok_total = [], True
    with open(log, "w", encoding="utf-8") as fh:
        for cmd in cmds:
            inicio = datetime.now()
            proc = await asyncio.create_subprocess_shell(cmd, cwd=str(carpeta), stdout=asyncio.subprocess.PIPE,
                                                         stderr=asyncio.subprocess.STDOUT)
            try:
                out, _ = await asyncio.wait_for(proc.communicate(), timeout=VERIF_TIMEOUT)
                code = proc.returncode
            except asyncio.TimeoutError:
                proc.kill()
                out, code = f"Timeout tras {VERIF_TIMEOUT}s".encode(), -1
            texto = out.decode("utf-8", errors="replace")
            fh.write(f"$ {cmd}\n(exit {code}, {(datetime.now() - inicio).seconds}s)\n{texto}\n\n")
            ok = code == 0
            ok_total &= ok
            cola = "\n".join(l for l in texto.strip().splitlines()[-15:]) if not ok else ""
            lineas.append(f"{'✅' if ok else '❌'} {cmd} (exit {code})" + (f"\n{cola}" if cola else ""))
    estado = "VERDE" if ok_total else "ROJO"
    return "\n".join([f"Verificación {estado} en {carpeta} (comandos {origen}):", *avisos, *lineas,
                      f"Log completo: {log}"])


# ----------------------------------------------------------------------------- trabajo aislado (worktrees)

def _leer_wt() -> dict:
    try:
        return json.loads(WORKTREES_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _guardar_wt(data: dict) -> None:
    WORKTREES_FILE.parent.mkdir(parents=True, exist_ok=True)
    WORKTREES_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def repo_root(cwd: Path) -> Path | None:
    r = _git(["rev-parse", "--show-toplevel"], cwd)
    return Path(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip() else None


def worktree_info(path: Path) -> dict | None:
    """Datos del worktree del orquestador que contiene `path` (o None si no es uno)."""
    p = str(path.resolve()).lower()
    for wt, info in _leer_wt().items():
        if p == wt.lower() or p.startswith(wt.lower() + os.sep):
            return {"worktree": wt, **info}
    return None


def crear_worktree(target: Path, slug: str, base_dir: Path) -> dict | None:
    """Crea un worktree en una rama orq/<slug> desde HEAD del repo de `target`. None si no es repo git."""
    cwd = target if target.is_dir() else target.parent
    root = repo_root(cwd)
    if not root:
        return None
    base = _git(["rev-parse", "HEAD"], root).stdout.strip()
    if not base:
        return None  # repo sin commits
    rama = f"orq/{slug}"
    wt = base_dir / ".orq-worktrees" / f"{root.name}-{slug}"
    wt.parent.mkdir(parents=True, exist_ok=True)
    r = _git(["worktree", "add", "-q", "-b", rama, str(wt), base], root, timeout=300)
    if r.returncode != 0:
        raise RuntimeError(f"git worktree add falló: {r.stderr.strip()[:300]}")
    rel = target.resolve().relative_to(root.resolve()) if target.resolve() != root.resolve() else Path(".")
    data = _leer_wt()
    data[str(wt.resolve())] = {"repo": str(root.resolve()), "rama": rama, "base": base, "creado": _now()}
    _guardar_wt(data)
    return {"worktree": wt.resolve(), "repo": root.resolve(), "rama": rama, "base": base,
            "target": (wt / rel).resolve()}


def confirmar_cambios(wt: Path, mensaje: str) -> str:
    """Hace commit en la rama orq/ de lo que dejó el subagente. Devuelve el sha corto o ""."""
    _git(["add", "-A"], wt)
    if not _git(["status", "--porcelain"], wt).stdout.strip():
        return ""
    r = _git(["commit", "-q", "-m", mensaje[:200]], wt)
    if r.returncode != 0:  # sin identidad git configurada: usa una genérica solo para esta rama
        r = _git(["-c", "user.name=Orquestador", "-c", "user.email=orquestador@localhost",
                  "commit", "-q", "-m", mensaje[:200]], wt)
    return _git(["rev-parse", "--short", "HEAD"], wt).stdout.strip() if r.returncode == 0 else ""


def integrar(worktree: str, accion: str) -> str:
    """accion: 'aplicar' (squash al repo original, cambios en staging sin commit) | 'descartar' | 'conservar'."""
    info = worktree_info(Path(worktree))
    if not info:
        return f"Error: {worktree} no es un worktree del orquestador."
    wt, repo, rama = Path(info["worktree"]), Path(info["repo"]), info["rama"]
    if accion == "conservar":
        return f"Se conserva {wt} (rama {rama}) para revisarlo después."
    if accion == "aplicar":
        r = _git(["merge", "--squash", rama], repo, timeout=300)
        if r.returncode != 0:
            return ("No se pudo aplicar (¿cambios locales en los mismos archivos o conflicto?). Nada se perdió: "
                    f"la rama {rama} sigue en {wt}.\n{(r.stdout + r.stderr).strip()[-600:]}")
    elif accion != "descartar":
        return "Error: accion debe ser aplicar, descartar o conservar."
    _git(["worktree", "remove", "--force", str(wt)], repo, timeout=120)
    _git(["branch", "-D", rama], repo)
    data = _leer_wt()
    data.pop(str(wt), None)
    _guardar_wt(data)
    if accion == "aplicar":
        stat = _git(["diff", "--cached", "--stat"], repo).stdout.strip().splitlines()
        return (f"Aplicado en {repo}: los cambios quedaron en staging, SIN commit (revísalos y haz commit tú).\n"
                + "\n".join(stat[-12:]) + f"\nWorktree y rama {rama} eliminados.")
    return f"Descartado: worktree {wt} y rama {rama} eliminados. Tu repo no cambió."


def listar_worktrees() -> str:
    data = _leer_wt()
    if not data:
        return "No hay trabajos aislados pendientes."
    return "\n".join(f"- {wt} · rama {i['rama']} · repo {i['repo']} · desde {i['creado']}" for wt, i in data.items())


# ----------------------------------------------------------------------------- presupuesto de Innti

def _limite(nombre: str) -> float:
    try:
        return float(os.getenv(nombre) or 0)
    except ValueError:
        return 0.0


def gasto() -> dict:
    hoy, mes = datetime.now().date().isoformat(), datetime.now().strftime("%Y-%m")
    out = {"hoy": 0.0, "mes": 0.0}
    if registro.COSTOS_FILE.exists():
        for line in registro.COSTOS_FILE.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            ts, c = r.get("ts", ""), r.get("costo_usd", 0)
            if ts.startswith(mes):
                out["mes"] += c
                if ts.startswith(hoy):
                    out["hoy"] += c
    return out


def estado_presupuesto() -> tuple[bool, str]:
    """(bloqueado, mensaje). Bloquea al alcanzar el límite diario o mensual; avisa desde PRESUPUESTO_AVISO_PCT."""
    g, dia, mes = gasto(), _limite("PRESUPUESTO_DIA_USD"), _limite("PRESUPUESTO_MES_USD")
    aviso = _limite("PRESUPUESTO_AVISO_PCT") or 80
    partes, bloqueado, alerta = [], False, False
    for etiqueta, usado, limite in (("hoy", g["hoy"], dia), ("mes", g["mes"], mes)):
        if limite <= 0:
            partes.append(f"{etiqueta}: US$ {usado:.4f} (sin límite)")
            continue
        pct = 100 * usado / limite
        partes.append(f"{etiqueta}: US$ {usado:.4f} de {limite:.2f} ({pct:.0f}%)")
        bloqueado |= usado >= limite
        alerta |= pct >= aviso
    icono = "⛔" if bloqueado else "⚠" if alerta else "✅"
    return bloqueado, f"{icono} Presupuesto Innti · " + " · ".join(partes)
