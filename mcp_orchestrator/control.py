"""Control de calidad y gasto: verificación por proyecto, reglas por proyecto y presupuesto de Innti."""

from __future__ import annotations

import asyncio
import json
import os
import re
import threading
import unicodedata
from datetime import datetime
from pathlib import Path

import plataforma
import registro

VERIF_FILE = Path(os.getenv("VERIFICACION_FILE") or plataforma.DATOS_DIR / "verificacion.json")
REGLAS_FILE = Path(os.getenv("REGLAS_FILE") or plataforma.DATOS_DIR / "reglas.json")
REGLAS_CANDIDATAS_FILE = Path(os.getenv("REGLAS_CANDIDATAS_FILE") or plataforma.DATOS_DIR / "reglas_candidatas.json")
VERIF_TIMEOUT = int(os.getenv("VERIFICACION_TIMEOUT_SECONDS") or "900")
UMBRAL_REGLA_RECURRENTE = 2
MAX_REGLAS_POR_PROYECTO = 15
MAX_LONGITUD_REGLA = 160

# ----------------------------------------------------------------------------- persistencia atómica y tolerancia

def _leer_json_seguro(ruta: Path) -> dict:
    if not ruta.exists():
        return {}
    try:
        data = json.loads(ruta.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
        return {}
    except (OSError, ValueError):
        return {}


def _escribir_json_atomico(ruta: Path, data: dict) -> None:
    ruta.parent.mkdir(parents=True, exist_ok=True)
    tmp = ruta.with_suffix(f".tmp.{os.urandom(4).hex()}")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(ruta)
    except Exception:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        raise


def _normalizar_proyecto(proyecto: str) -> str:
    return (proyecto or "").strip()


# ----------------------------------------------------------------------------- similitud y dedup de reglas

PALABRAS_VACIAS = {
    "a", "al", "algo", "algunas", "algunos", "ante", "antes", "como", "con", "contra",
    "cual", "cuando", "de", "del", "desde", "donde", "durante", "e", "el", "ella",
    "ellos", "en", "entre", "era", "es", "esa", "ese", "eso", "esta", "estar",
    "estas", "este", "estos", "esto", "fue", "ha", "han", "hasta", "hay", "la",
    "las", "le", "les", "lo", "los", "mas", "me", "mi", "mis", "mucho",
    "muchos", "muy", "nada", "ni", "nos", "nosotros", "o", "otra", "otras", "otro",
    "otros", "para", "pero", "poco", "por", "porque", "que", "quien", "quienes", "se",
    "si", "sin", "sobre", "son", "su", "sus", "tambien", "tanto", "te", "tiene",
    "tienen", "todo", "todos", "tu", "tus", "un", "una", "unas", "uno", "unos",
    "ya", "yo",
    "the", "in", "for", "to", "of", "and", "an", "on", "with", "at", "by", "from",
}


def _sin_tildes(texto: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", texto) if unicodedata.category(c) != "Mn")


def tokens_regla(texto: str) -> set[str]:
    t = _sin_tildes(texto.lower())
    t = re.sub(r"[^\w\s]", " ", t)
    palabras = t.split()
    filtradas = {p for p in palabras if p not in PALABRAS_VACIAS and len(p) > 0}
    return filtradas if filtradas else set(palabras)


def similitud_jaccard(texto1: str, texto2: str) -> float:
    toks1 = tokens_regla(texto1)
    toks2 = tokens_regla(texto2)
    if not toks1 and not toks2:
        return 1.0
    if not toks1 or not toks2:
        return 0.0
    inter = len(toks1 & toks2)
    union = len(toks1 | toks2)
    return inter / union if union else 0.0


# ----------------------------------------------------------------------------- reglas por proyecto

def _leer_reglas() -> dict[str, list[str]]:
    return _leer_json_seguro(REGLAS_FILE)


def _leer_candidatas() -> dict[str, list[dict]]:
    return _leer_json_seguro(REGLAS_CANDIDATAS_FILE)


def guardar_reglas(proyecto: str, reglas: list[str]) -> str:
    proy = _normalizar_proyecto(proyecto)
    data = _leer_reglas()
    limpias = [r.strip() for r in (reglas or []) if r and r.strip()]
    if limpias:
        data[proy] = limpias
    else:
        data.pop(proy, None)
    _escribir_json_atomico(REGLAS_FILE, data)
    return (f"Reglas de '{proy}' guardadas ({len(limpias)}): " + " | ".join(limpias) if limpias
            else f"Reglas de '{proy}' eliminadas.")


def recordar_regla(proyecto: str, regla: str) -> str:
    proy = _normalizar_proyecto(proyecto)
    texto = (regla or "").strip()
    if not proy:
        return "Error: debes indicar el proyecto."
    if not texto:
        return "Error: la regla no puede estar vacía."
    if len(texto) > MAX_LONGITUD_REGLA:
        return (f"Rechazada: la regla supera el límite de {MAX_LONGITUD_REGLA} caracteres "
                f"({len(texto)} caracteres). Debe ser corta e imperativa.")

    # 1) Comprobar si ya existe en las reglas fijas por similitud
    data_reglas = _leer_reglas()
    fijas = data_reglas.get(proy, [])
    for r in fijas:
        if similitud_jaccard(r, texto) >= 0.7:
            return "ya existía"

    # 2) Buscar o registrar en candidatas
    data_cand = _leer_candidatas()
    cands = data_cand.get(proy, [])
    match = None
    for c in cands:
        if similitud_jaccard(c.get("regla", ""), texto) >= 0.7:
            match = c
            break

    ahora = datetime.now().isoformat(timespec="seconds")
    if match:
        match["veces"] = match.get("veces", 1) + 1
        match["ultima"] = ahora
        if len(texto) < len(match.get("regla", "")):
            match["regla"] = texto
        candidato = match
    else:
        candidato = {"regla": texto, "veces": 1, "ultima": ahora}
        cands.append(candidato)

    # 3) Promoción automática si alcanza el umbral
    if candidato["veces"] >= UMBRAL_REGLA_RECURRENTE:
        if len(fijas) >= MAX_REGLAS_POR_PROYECTO:
            data_cand[proy] = cands
            _escribir_json_atomico(REGLAS_CANDIDATAS_FILE, data_cand)
            return (f"Tope alcanzado: '{proy}' ya tiene {MAX_REGLAS_POR_PROYECTO} reglas fijas. "
                    f"No se promovió.")

        # Quitar de candidatas
        cands = [c for c in cands if c is not candidato]
        if cands:
            data_cand[proy] = cands
        else:
            data_cand.pop(proy, None)
        _escribir_json_atomico(REGLAS_CANDIDATAS_FILE, data_cand)

        # Agregar a fijas (verificando que no duplique por similitud)
        regla_final = candidato["regla"]
        if not any(similitud_jaccard(r, regla_final) >= 0.7 for r in fijas):
            fijas.append(regla_final)
            data_reglas[proy] = fijas
            _escribir_json_atomico(REGLAS_FILE, data_reglas)
            return f"promovida a regla fija de {proy}"
        else:
            return "ya existía"
    else:
        data_cand[proy] = cands
        _escribir_json_atomico(REGLAS_CANDIDATAS_FILE, data_cand)
        return f"candidata {candidato['veces']}/{UMBRAL_REGLA_RECURRENTE}"


def listar_reglas(proyecto: str = "") -> str:
    proy = _normalizar_proyecto(proyecto)
    data_reglas = _leer_reglas()
    data_cand = _leer_candidatas()

    if proy:
        fijas = data_reglas.get(proy, [])
        cands = data_cand.get(proy, [])
        if not fijas and not cands:
            return f"Sin reglas fijas ni candidatas para '{proy}'."
        bloques = [f"Reglas de '{proy}':"]
        bloques.append("  Reglas fijas:")
        if fijas:
            for r in fijas:
                bloques.append(f"    - {r}")
        else:
            bloques.append("    (ninguna)")
        bloques.append("  Reglas candidatas:")
        if cands:
            for c in cands:
                bloques.append(f"    - {c['regla']} ({c.get('veces', 1)}/{UMBRAL_REGLA_RECURRENTE}, última: {c.get('ultima', '-')})")
        else:
            bloques.append("    (ninguna)")
        return "\n".join(bloques)

    proyectos = sorted(set(data_reglas.keys()) | set(data_cand.keys()))
    if not proyectos:
        return "Sin reglas fijas ni candidatas registradas."

    salida = []
    for p in proyectos:
        salida.append(f"Proyecto '{p}':")
        fijas = data_reglas.get(p, [])
        cands = data_cand.get(p, [])
        salida.append("  Reglas fijas:")
        if fijas:
            for r in fijas:
                salida.append(f"    - {r}")
        else:
            salida.append("    (ninguna)")
        salida.append("  Reglas candidatas:")
        if cands:
            for c in cands:
                salida.append(f"    - {c['regla']} ({c.get('veces', 1)}/{UMBRAL_REGLA_RECURRENTE}, última: {c.get('ultima', '-')})")
        else:
            salida.append("    (ninguna)")
    return "\n".join(salida)


def olvidar_regla(proyecto: str, texto: str) -> str:
    proy = _normalizar_proyecto(proyecto)
    busqueda = (texto or "").strip()
    if not proy:
        return "Error: debes indicar el proyecto."
    if not busqueda:
        return "Error: debes indicar el texto de la regla a olvidar."

    borradas_fijas = []
    borradas_cand = []

    # 1) Buscar en fijas
    data_reglas = _leer_reglas()
    if proy in data_reglas:
        nuevas_fijas = []
        for r in data_reglas[proy]:
            if similitud_jaccard(r, busqueda) >= 0.7:
                borradas_fijas.append(r)
            else:
                nuevas_fijas.append(r)
        if borradas_fijas:
            if nuevas_fijas:
                data_reglas[proy] = nuevas_fijas
            else:
                data_reglas.pop(proy, None)
            _escribir_json_atomico(REGLAS_FILE, data_reglas)

    # 2) Buscar en candidatas
    data_cand = _leer_candidatas()
    if proy in data_cand:
        nuevas_cand = []
        for c in data_cand[proy]:
            if similitud_jaccard(c.get("regla", ""), busqueda) >= 0.7:
                borradas_cand.append(c.get("regla", ""))
            else:
                nuevas_cand.append(c)
        if borradas_cand:
            if nuevas_cand:
                data_cand[proy] = nuevas_cand
            else:
                data_cand.pop(proy, None)
            _escribir_json_atomico(REGLAS_CANDIDATAS_FILE, data_cand)

    if not borradas_fijas and not borradas_cand:
        return f"No se encontró ninguna regla similar a '{busqueda}' en '{proy}'."

    detalles = []
    if borradas_fijas:
        detalles.append(f"fijas: {', '.join(borradas_fijas)}")
    if borradas_cand:
        detalles.append(f"candidatas: {', '.join(borradas_cand)}")
    return f"Regla olvidada en '{proy}': " + "; ".join(detalles)


def reglas_para(ruta: Path | str) -> list[str]:
    """Reglas obligatorias configuradas para un proyecto (por ruta o por nombre de proyecto)."""
    if not ruta:
        return []
    data = _leer_reglas()
    p = Path(ruta)
    try:
        r = str(p.resolve()).lower()
    except OSError:
        r = str(p).lower()
    for clave, reglas in sorted(data.items(), key=lambda kv: -len(kv[0])):
        if "\\" in clave or "/" in clave:
            try:
                pref = str(Path(clave).resolve()).lower()
            except OSError:
                pref = clave.lower()
            if r.startswith(pref):
                return list(reglas)
    proyecto = registro.proyecto_de(str(p))
    if proyecto in data:
        return list(data[proyecto])
    partes = {parte.lower() for parte in p.parts} | {p.name.lower(), str(ruta).strip().lower()}
    for clave, reglas in data.items():
        if clave.lower() in partes:
            return list(reglas)
    return []


# ----------------------------------------------------------------------------- verificación por proyecto

def _leer_verif() -> dict:
    return _leer_json_seguro(VERIF_FILE)


def guardar_verificacion(proyecto: str, comandos: list[str]) -> str:
    proy = _normalizar_proyecto(proyecto)
    data = _leer_verif()
    if comandos:
        data[proy] = comandos
    else:
        data.pop(proy, None)
    _escribir_json_atomico(VERIF_FILE, data)
    return (f"Verificación de '{proy}' guardada: {' && '.join(comandos)}" if comandos
            else f"Verificación de '{proy}' eliminada (se volverá a detectar sola).")


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
    try:
        rel = ruta.resolve().relative_to(tope.resolve())
        proy_tope = rel.parts[0] if rel.parts else ""
    except ValueError:
        proy_tope = ""
    proyecto = proy_tope if proy_tope in data else registro.proyecto_de(str(ruta))
    if proyecto not in data and ruta.name in data:
        proyecto = ruta.name
    if proyecto in data:
        # Se ejecuta en la raíz del proyecto, no en la carpeta de trabajo.
        raiz = tope / proyecto
        carpeta = raiz if raiz.is_dir() else (ruta if ruta.is_dir() else tope)
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
                await proc.wait()
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


_VERIF_BG: dict[str, dict] = {}


def iniciar_verificacion_segundo_plano(ruta: Path, tope: Path, logs_dir: Path, proyecto: str = "") -> tuple[str, str]:
    """Lanza `verificar` en un hilo aparte y devuelve (id, mensaje)."""
    vid = f"verif-{datetime.now():%Y%m%d-%H%M%S}-{os.urandom(2).hex()}"
    nombre = proyecto or registro.proyecto_de(str(ruta))
    info: dict = {"id": vid, "proyecto": nombre, "ruta": str(ruta), "estado": "en_curso", "resultado": "",
                  "iniciado": datetime.now().isoformat(timespec="seconds")}

    def _worker() -> None:
        try:
            res = asyncio.run(verificar(ruta, tope, None, logs_dir))
            info["resultado"] = res
            info["estado"] = "terminado"
        except Exception as e:  # pragma: no cover
            info["resultado"] = f"Error en verificación de '{nombre}': {type(e).__name__}: {e}"
            info["estado"] = "error"

    hilo = threading.Thread(target=_worker, name=f"verif-{vid}", daemon=True)
    info["thread"] = hilo
    _VERIF_BG[vid] = info
    hilo.start()
    msg = (f"verificacion_id: {vid}\nVerificación en segundo plano iniciada para '{nombre}' ({ruta}). "
           f"Consulta luego con resultado_verificacion('{vid}').")
    return vid, msg


def consultar_verificacion_segundo_plano(vid: str) -> str:
    """Devuelve el estado ('En curso...') o el resultado final de una verificación en segundo plano."""
    info = _VERIF_BG.get((vid or "").strip())
    if not info:
        return f"Error: no existe ninguna verificación con id '{vid}'."
    if info["estado"] == "en_curso":
        return f"En curso: la verificación '{vid}' ({info['proyecto']}) sigue ejecutándose."
    return info["resultado"]


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
