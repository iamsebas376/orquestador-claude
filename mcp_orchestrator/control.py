"""Control de calidad y gasto: verificación por proyecto y presupuesto de Innti."""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime
from pathlib import Path

import plataforma
import registro

VERIF_FILE = Path(os.getenv("VERIFICACION_FILE") or plataforma.REPO_ROOT / "verificacion.json")
VERIF_TIMEOUT = int(os.getenv("VERIFICACION_TIMEOUT_SECONDS") or "900")


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
            prog = ".\\mvnw.cmd" if (d / "mvnw.cmd").exists() else "mvn"
            cmds.append(prog + " -q -B test")
        elif any((d / g).exists() for g in ("build.gradle", "build.gradle.kts")):
            prog = ".\\gradlew.bat" if (d / "gradlew.bat").exists() else "gradle"
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
    r = str(ruta.resolve()).lower()
    for clave, cmds in sorted(data.items(), key=lambda kv: -len(kv[0])):
        if ("\\" in clave or "/" in clave) and r.startswith(str(Path(clave).resolve()).lower()):
            return Path(clave), cmds, "configurados"
    proyecto = registro.proyecto_de(str(ruta))
    if proyecto in data:
        # Se ejecuta en la raíz del proyecto, no en la carpeta de trabajo.
        raiz = tope / proyecto
        carpeta = raiz if raiz.is_dir() else ruta
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
        avisos.append("⚠ no hay node_modules en esta carpeta: ejecuta la instalación de "
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
