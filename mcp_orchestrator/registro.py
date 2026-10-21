"""Registro del orquestador: checklists por pedido, costos de delegaciones y aprendizajes por proyecto.

Todo se guarda como archivos en disco (JSON + Markdown legible) para que sobreviva entre sesiones de Claude.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
REPO_ROOT = BASE_DIR.parent
TAREAS_DIR = Path(os.getenv("TAREAS_DIR") or REPO_ROOT / "tareas")
LOGS_DIR = Path(os.getenv("LOGS_DIR") or REPO_ROOT / "logs")
COSTOS_FILE = LOGS_DIR / "costos.jsonl"
APRENDIZAJES_FILE = Path(os.getenv("APRENDIZAJES_FILE") or REPO_ROOT / "aprendizajes.jsonl")
MAX_RONDAS = int(os.getenv("MAX_RONDAS_CORRECCION") or "2")

ESTADOS = {
    "pendiente": "⬜", "en_curso": "🔄", "revisando": "🔍", "corrigiendo": "🔁",
    "hecho": "✅", "fallido": "❌", "omitido": "⏭️",
}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _slug(text: str, n: int = 40) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", text.lower()).strip("-")
    return (s[:n] or "pedido").strip("-")


def _usd(x: float) -> str:
    return f"US$ {x:.4f}"


# ----------------------------------------------------------------------------- precios y costos

PRECIOS_FILE = Path(os.getenv("PRECIOS_FILE") or BASE_DIR / "precios.json")  # local; ver precios.example.json


def _precios() -> dict:
    try:
        return json.loads(PRECIOS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _precio_vigente(modelo: str, fecha: str = "") -> list | None:
    """[entrada, caché, salida] del modelo en la fecha dada (ISO; vacío = hoy).
    Acepta vigencias ({desde, precio}) y los sufijos de esfuerzo de agy (-low/-medium/-high)."""
    tabla = _precios()
    nombre = modelo.split("/")[-1]
    precio = tabla.get(nombre) or tabla.get(re.sub(r"-(low|medium|high|max)$", "", nombre))
    if isinstance(precio, list) and precio and isinstance(precio[0], dict):
        dia = (fecha or _now())[:10]
        vigentes = [v for v in precio if v.get("desde", "") <= dia]
        precio = max(vigentes, key=lambda v: v["desde"])["precio"] if vigentes else None
    return precio if precio and len(precio) == 3 else None


def costo_innti(modelo: str, tokens: dict, fecha: str = "") -> float:
    """Costo estimado en US$ con la tabla de precios (vigente en `fecha`). El razonamiento se cobra como salida."""
    precio = _precio_vigente(modelo, fecha)
    if not precio:
        return 0.0
    entrada, cache, salida = precio
    out = tokens.get("output", 0) + tokens.get("reasoning", 0)
    return (tokens.get("input", 0) * entrada + tokens.get("cache_read", 0) * cache + out * salida) / 1_000_000


def registrar_costo(agente: str, modelo: str, tokens: dict, costo: float, pedido_id: str = "", item: int = 0,
                    ruta: str = "", equivalente: float = 0.0, sesion: str = "") -> None:
    """costo_usd = gasto real (cuenta para el presupuesto). costo_equivalente_usd = lo que costaría por la API
    un agente sin cobro por token (Antigravity con plan Pro); se informa aparte y no frena nada."""
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    fila = {"ts": _now(), "agente": agente, "modelo": modelo, "tokens": tokens, "costo_usd": round(costo, 6),
            "pedido_id": pedido_id, "item": item, "ruta": ruta}
    if sesion:  # sesión de OpenCode o conversación de Antigravity: el panel cuenta sesiones distintas
        fila["sesion"] = sesion
    if equivalente:
        fila["costo_equivalente_usd"] = round(equivalente, 6)
    with open(COSTOS_FILE, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(fila, ensure_ascii=False) + "\n")


def resumen_costos(dias: int = 1) -> str:
    """Consumo de las delegaciones de los últimos `dias` días: total, por agente y por modelo."""
    desde = datetime.now() - timedelta(days=dias)
    filas = []
    if COSTOS_FILE.exists():
        for line in COSTOS_FILE.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if datetime.fromisoformat(r["ts"]) >= desde:
                filas.append(r)
    if not filas:
        return f"Sin delegaciones registradas en los últimos {dias} día(s)."
    total = sum(r["costo_usd"] for r in filas)
    equiv = sum(r.get("costo_equivalente_usd", 0) for r in filas)
    por_modelo: dict[str, list] = {}
    for r in filas:
        k = f"{r['agente']} · {r['modelo']}"
        acc = por_modelo.setdefault(k, [0, 0, 0, 0.0, 0.0])
        t = r.get("tokens") or {}
        acc[0] += 1
        acc[1] += t.get("input", 0) + t.get("cache_read", 0)
        acc[2] += t.get("output", 0) + t.get("reasoning", 0)
        acc[3] += r["costo_usd"]
        acc[4] += r.get("costo_equivalente_usd", 0)
    lineas = [f"Últimos {dias} día(s): {len(filas)} delegaciones · gasto real {_usd(total)}"
              + (f" · Antigravity equivalente API ≈ {_usd(equiv)}" if equiv else "")]
    for k, (n, ti, to, c, e) in sorted(por_modelo.items(), key=lambda x: -(x[1][3] + x[1][4])):
        monto = _usd(c) if c else (f"≈ {_usd(e)} equivalente" if e else _usd(0))
        lineas.append(f"- {k}: {n} tareas · {ti:,} tokens entrada · {to:,} salida · {monto}".replace(",", "."))
    lineas.append("(Innti: gasto estimado con la tabla de precios. Antigravity: plan Pro sin cobro por token; "
                  "el equivalente es lo que costaría por la API de Gemini)")
    return "\n".join(lineas)


# ----------------------------------------------------------------------------- pedidos (checklists)

def _path(pid: str) -> Path:
    return TAREAS_DIR / f"{pid}.json"


def _load(pid: str) -> dict | None:
    try:
        return json.loads(_path(pid).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _render(p: dict) -> str:
    hechos = sum(1 for i in p["items"] if i["estado"] in ("hecho", "omitido"))
    costo = sum(i.get("costo_usd", 0) for i in p["items"])
    lines = [f"# {p['titulo']}", "",
             f"- Pedido: `{p['id']}` · estado: **{p['estado']}** · progreso: {hechos}/{len(p['items'])} · "
             f"costo: {_usd(costo)}",
             f"- Creado: {p['creado']} · actualizado: {p['actualizado']}"]
    if p.get("ruta"):
        lines.append(f"- Ruta: `{p['ruta']}`")
    if p.get("contexto"):
        lines += ["", f"> {p['contexto']}"]
    lines += ["", "## Checklist", ""]
    for n, i in enumerate(p["items"], 1):
        meta = " · ".join(x for x in (i.get("agente"), i.get("modelo"),
                                      f"ronda {i['rondas']}" if i.get("rondas") else "",
                                      _usd(i["costo_usd"]) if i.get("costo_usd") else "") if x)
        lines.append(f"{n}. {ESTADOS.get(i['estado'], '⬜')} {i['texto']}" + (f"  — {meta}" if meta else ""))
        for nota in i.get("notas", [])[-3:]:
            lines.append(f"   - {nota}")
    if p.get("resumen"):
        lines += ["", "## Resumen final", "", p["resumen"]]
    return "\n".join(lines) + "\n"


def _save(p: dict) -> None:
    TAREAS_DIR.mkdir(parents=True, exist_ok=True)
    p["actualizado"] = _now()
    _path(p["id"]).write_text(json.dumps(p, ensure_ascii=False, indent=2), encoding="utf-8")
    (TAREAS_DIR / f"{p['id']}.md").write_text(_render(p), encoding="utf-8")


_RUTA_EN_TITULO = re.compile(r"\s*[\(\[]?\s*((?:[A-Za-z]:[\\/]|\\\\|~[\\/]|/)[^()\[\]]*?)\s*[\)\]]?\s*$")


def separar_ruta(titulo: str) -> tuple[str, str]:
    """El título del pedido es solo el nombre: si trae una ruta (p. ej. '… (D:\\personal\\x)'), la separa."""
    m = _RUTA_EN_TITULO.search(titulo)
    if not m:
        return titulo.strip(), ""
    limpio = titulo[: m.start()].rstrip(" -–—·:,").strip()
    return (limpio or titulo.strip()), (m.group(1).strip() if limpio else "")


def iniciar_pedido(titulo: str, items: list[str], contexto: str = "", ruta: str = "") -> str:
    """Crea el checklist de un pedido del usuario. Devuelve el pedido_id para vincular delegaciones.
    titulo: nombre corto, sin rutas (si trae una, se mueve a `ruta`)."""
    titulo, ruta_titulo = separar_ruta(titulo)
    ruta = ruta or ruta_titulo
    pid = f"{datetime.now():%Y%m%d-%H%M%S}-{_slug(titulo, 30)}"
    p = {"id": pid, "titulo": titulo, "ruta": ruta, "contexto": contexto, "estado": "abierto", "creado": _now(),
         "actualizado": _now(), "resumen": "",
         "items": [{"texto": t, "estado": "pendiente", "rondas": 0, "costo_usd": 0.0, "notas": []} for t in items]}
    _save(p)
    return f"pedido_id: {pid}\nChecklist: {TAREAS_DIR / (pid + '.md')}\n\n{_render(p)}"


def agregar_items(pedido_id: str, items: list[str]) -> str:
    p = _load(pedido_id)
    if not p:
        return f"Error: no existe el pedido {pedido_id}."
    p["items"] += [{"texto": t, "estado": "pendiente", "rondas": 0, "costo_usd": 0.0, "notas": []} for t in items]
    _save(p)
    return _render(p)


def actualizar_item(pedido_id: str, item: int, estado: str, nota: str = "", agente: str = "", modelo: str = "",
                    costo_usd: float = 0.0) -> str:
    """Cambia el estado de un ítem (1 = primero). 'corrigiendo' suma una ronda y avisa si se pasa del límite."""
    p = _load(pedido_id)
    if not p:
        return f"Error: no existe el pedido {pedido_id}."
    if estado not in ESTADOS:
        return f"Error: estado inválido. Usa uno de: {', '.join(ESTADOS)}."
    if not 1 <= item <= len(p["items"]):
        return f"Error: el pedido tiene {len(p['items'])} ítems."
    it = p["items"][item - 1]
    aviso = ""
    if estado == "corrigiendo":
        it["rondas"] = it.get("rondas", 0) + 1
        if it["rondas"] > MAX_RONDAS:
            aviso = (f"\n⚠ Límite alcanzado: el ítem {item} ya lleva {it['rondas']} rondas de corrección "
                     f"(máximo {MAX_RONDAS}). Detente y consulta al usuario antes de volver a delegar.")
    it["estado"] = estado
    it.setdefault("inicio", _now())  # ventana del ítem: sirve para repartirle el trabajo de Claude
    it["ultimo"] = _now()
    if agente:
        it["agente"] = agente
    if modelo:
        it["modelo"] = modelo
    if costo_usd:
        it["costo_usd"] = round(it.get("costo_usd", 0) + costo_usd, 6)
    if nota:
        it.setdefault("notas", []).append(f"{datetime.now():%H:%M} {nota}")
    _save(p)
    return _render(p) + aviso


def cerrar_pedido(pedido_id: str, resumen: str, estado: str = "cerrado") -> str:
    p = _load(pedido_id)
    if not p:
        return f"Error: no existe el pedido {pedido_id}."
    p["estado"], p["resumen"] = estado, resumen
    _save(p)
    return _render(p)


def ver_pedido(pedido_id: str = "") -> str:
    if not pedido_id:
        files = sorted(TAREAS_DIR.glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True)
        if not files:
            return "No hay pedidos registrados."
        pedido_id = files[0].stem
    p = _load(pedido_id)
    return _render(p) if p else f"Error: no existe el pedido {pedido_id}."


def listar_pedidos(solo_abiertos: bool = True, limite: int = 15) -> str:
    files = sorted(TAREAS_DIR.glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True)
    out = []
    for f in files:
        p = _load(f.stem)
        if not p or (solo_abiertos and p["estado"] != "abierto"):
            continue
        hechos = sum(1 for i in p["items"] if i["estado"] in ("hecho", "omitido"))
        out.append(f"- `{p['id']}` · {p['titulo']} · {p['estado']} · {hechos}/{len(p['items'])}")
        if len(out) >= limite:
            break
    return "\n".join(out) or ("No hay pedidos abiertos." if solo_abiertos else "No hay pedidos.")


def item_automatico(ruta: str, texto: str) -> tuple[str, int, str]:
    """Para una delegación sin pedido (un ajuste o seguimiento): la agrega como ítem nuevo al pedido más reciente
    del mismo proyecto (prefiriendo el abierto, que se reabre si estaba cerrado) o crea un pedido de ajustes.
    Devuelve (pedido_id, ítem, aviso)."""
    def norm(r):
        return os.path.normcase(os.path.normpath(r)) if r else ""

    def mismo(a, b):
        return bool(a and b) and (a == b or a.startswith(b + os.sep) or b.startswith(a + os.sep))

    texto = " ".join(texto.split())[:120] or "Ajuste"
    cand = [p for f in TAREAS_DIR.glob("*.json") if (p := _load(f.stem)) and mismo(norm(ruta), norm(p.get("ruta", "")))]
    if cand:
        p = max(cand, key=lambda p: (p["estado"] == "abierto", p.get("creado") or ""))
        if p["estado"] != "abierto":
            p["estado"] = "abierto"
            _save(p)
        agregar_items(p["id"], [texto])
        p = _load(p["id"])
        return p["id"], len(p["items"]), f"\nChecklist: ajuste agregado como ítem {len(p['items'])} del pedido «{p['titulo']}»."
    proyecto = Path(ruta).name or "proyecto"
    pid = iniciar_pedido(f"Ajustes en {proyecto}", [texto], ruta=ruta).split("\n")[0].split(": ")[1]
    return pid, 1, f"\nChecklist: no había pedido de {proyecto}; se creó «Ajustes en {proyecto}» con este ajuste."


def vincular_delegacion(pedido_id: str, item: int, agente: str, modelo: str, costo: float, resultado: str) -> str:
    """Tras una delegación: pasa el ítem a 'revisando', suma costo y deja nota. Devuelve aviso para Claude."""
    if not pedido_id or not item:
        # Sin pedido el trabajo no aparece en el checklist ni en el panel (solo en los totales del día).
        return ("\n⚠ Delegación sin pedido: no queda en el checklist ni en el panel. Si es parte de un pedido "
                "abierto, usa agregar_items y vuelve a pasar pedido_id e item; si es un encargo nuevo, iniciar_pedido.")
    p = _load(pedido_id)
    if not p or not 1 <= item <= len(p["items"]):
        return f"\n(No se pudo vincular al pedido {pedido_id}, ítem {item}.)"
    actualizar_item(pedido_id, item, "revisando", nota=resultado[:160], agente=agente, modelo=modelo,
                    costo_usd=costo)
    return f"\nChecklist: ítem {item} del pedido {pedido_id} → 🔍 revisando. Revisa y márcalo hecho o corrigiendo."


def costos_data() -> dict:
    """Totales de costo (hoy y 7 días) y desglose por agente/modelo de los últimos 7 días."""
    hoy, semana = datetime.now().date(), datetime.now() - timedelta(days=7)
    out = {"hoy_usd": 0.0, "semana_usd": 0.0, "delegaciones_semana": 0, "por_modelo": {}}
    if COSTOS_FILE.exists():
        for line in COSTOS_FILE.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
                ts = datetime.fromisoformat(r["ts"])
            except (ValueError, KeyError):
                continue
            if ts < semana:
                continue
            c, t = r.get("costo_usd", 0), r.get("tokens") or {}
            out["semana_usd"] += c
            out["delegaciones_semana"] += 1
            if ts.date() == hoy:
                out["hoy_usd"] += c
            k = f"{r['agente']}|{r['modelo']}"
            m = out["por_modelo"].setdefault(k, {"agente": r["agente"], "modelo": r["modelo"], "tareas": 0,
                                                 "tokens_entrada": 0, "tokens_salida": 0, "costo_usd": 0.0})
            m["tareas"] += 1
            m["tokens_entrada"] += t.get("input", 0) + t.get("cache_read", 0)
            m["tokens_salida"] += t.get("output", 0) + t.get("reasoning", 0)
            m["costo_usd"] += c
    out["por_modelo"] = sorted(out["por_modelo"].values(), key=lambda m: -m["costo_usd"])
    for k in ("hoy_usd", "semana_usd"):
        out[k] = round(out[k], 4)
    for m in out["por_modelo"]:
        m["costo_usd"] = round(m["costo_usd"], 4)
    return out


PANEL_DIAS = int(os.getenv("PANEL_DIAS") or "120")
PANEL_MAX_PEDIDOS = int(os.getenv("PANEL_MAX_PEDIDOS") or "200")


def costos_diarios(dias: int = PANEL_DIAS) -> list[dict]:
    """Costo agregado por día, agente y modelo (para filtrar por fechas en el panel)."""
    desde = datetime.now() - timedelta(days=dias)
    agg: dict[tuple, dict] = {}
    if COSTOS_FILE.exists():
        for line in COSTOS_FILE.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
                ts = datetime.fromisoformat(r["ts"])
            except (ValueError, KeyError):
                continue
            if ts < desde:
                continue
            k = (ts.date().isoformat(), r["agente"], r["modelo"])
            t = r.get("tokens") or {}
            a = agg.setdefault(k, {"dia": k[0], "agente": k[1], "modelo": k[2], "tareas": 0,
                                   "tokens_entrada": 0, "tokens_cache": 0, "tokens_salida": 0, "costo_usd": 0.0,
                                   "costo_equivalente_usd": 0.0, "sesiones": []})
            a["tareas"] += 1
            a["tokens_cache"] += t.get("cache_read", 0)
            # Sin sesión registrada (filas antiguas sin recuperar): cada ítem del pedido cuenta como una.
            ses = (r.get("sesion") or f"{r.get('pedido_id') or r['ts']}:{r.get('item', 0)}")[:12]
            if ses not in a["sesiones"]:
                a["sesiones"].append(ses)
            a["tokens_entrada"] += t.get("input", 0) + t.get("cache_read", 0)
            a["tokens_salida"] += t.get("output", 0) + t.get("reasoning", 0)
            a["costo_usd"] += r.get("costo_usd", 0)
            a["costo_equivalente_usd"] += r.get("costo_equivalente_usd", 0)
    filas = sorted(agg.values(), key=lambda a: a["dia"])
    for a in filas:
        a["costo_usd"] = round(a["costo_usd"], 5)
        a["costo_equivalente_usd"] = round(a["costo_equivalente_usd"], 5)
    return filas


def consumo_por_item() -> dict:
    """Tokens, gasto real y equivalente agregados por (pedido_id, ítem) desde el registro de consumo."""
    agg: dict[tuple, dict] = {}
    if COSTOS_FILE.exists():
        for line in COSTOS_FILE.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if not r.get("pedido_id"):
                continue
            t = r.get("tokens") or {}
            a = agg.setdefault((r["pedido_id"], r.get("item", 0)),
                               {"tokens_entrada": 0, "tokens_salida": 0, "costo_usd": 0.0, "costo_equivalente_usd": 0.0,
                                "agentes": {}, "ts": []})
            a["tokens_entrada"] += t.get("input", 0) + t.get("cache_read", 0)
            a["tokens_salida"] += t.get("output", 0) + t.get("reasoning", 0)
            a["costo_usd"] += r.get("costo_usd", 0)
            a["costo_equivalente_usd"] += r.get("costo_equivalente_usd", 0)
            a["ts"].append(r.get("ts", ""))
            g = a["agentes"].setdefault(r.get("agente", "?"), {"tokens": 0, "costo_usd": 0.0, "costo_equivalente_usd": 0.0,
                                                               "modelos": []})
            if r.get("modelo") and r["modelo"] not in g["modelos"]:
                g["modelos"].append(r["modelo"])
            g["tokens"] += sum(t.get(k, 0) for k in ("input", "cache_read", "output", "reasoning"))
            g["costo_usd"] += r.get("costo_usd", 0)
            g["costo_equivalente_usd"] += r.get("costo_equivalente_usd", 0)
    return agg


def _ventanas_items(p: dict, consumo: dict) -> list[tuple]:
    """(inicio, fin, nº de ítem) de cada ítem con actividad conocida: sus marcas de tiempo, la hora de sus notas
    (HH:MM, en el día del pedido o el siguiente) y los momentos de sus delegaciones."""
    try:
        creado = datetime.fromisoformat(p.get("creado") or p["actualizado"])
    except ValueError:
        return []
    out = []
    for n, i in enumerate(p["items"], 1):
        momentos = []
        for k in ("inicio", "ultimo"):
            try:
                momentos.append(datetime.fromisoformat(i[k]))
            except (KeyError, ValueError):
                pass
        for nota in i.get("notas", []):
            m = re.match(r"(\d{2}):(\d{2}) ", nota)
            if m:
                t = creado.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
                momentos.append(t if t >= creado - timedelta(minutes=1) else t + timedelta(days=1))
        for ts in consumo.get((p["id"], n), {}).get("ts", []):
            try:
                momentos.append(datetime.fromisoformat(ts))
            except ValueError:
                pass
        if momentos:
            out.append((min(momentos), max(momentos), n))
    return out


def _items_de(ts: datetime, ventanas: list[tuple]) -> list[int]:
    """Ítems entre los que se reparte una respuesta de Claude: los que estaban en curso en ese momento (a partes
    iguales si se solapan); si ninguno, el siguiente en empezar (Claude prepara la delegación antes); si no, el
    último en terminar."""
    if not ventanas:
        return []
    dentro = [n for ini, fin, n in ventanas if ini - timedelta(minutes=1) <= ts <= fin + timedelta(minutes=1)]
    if dentro:
        return dentro
    despues = [(ini, n) for ini, fin, n in ventanas if ini > ts]
    if despues:
        primero = min(despues)[0]
        return [n for ini, n in despues if ini == primero]
    ultimo = max(fin for ini, fin, n in ventanas)
    if ts - ultimo > timedelta(minutes=5):
        return [0]  # después del último ítem: trabajo de Claude sin ítem (no se le carga al último)
    return [n for ini, fin, n in ventanas if fin == ultimo]


# ----------------------------------------------------------------------------- consumo de Claude (el cerebro)

CLAUDE_PROJECTS_DIR = Path(os.getenv("CLAUDE_PROJECTS_DIR") or Path.home() / ".claude" / "projects")


def _mensajes_claude(desde: datetime) -> list[dict]:
    """Respuestas de Claude Code desde `desde` (hora local), leídas de sus transcripts locales
    (~/.claude/projects/**/*.jsonl, subagentes incluidos). Cada respuesta se repite por bloque: se deja una por id."""
    por_id: dict[str, dict] = {}
    for f in CLAUDE_PROJECTS_DIR.rglob("*.jsonl"):
        try:
            if datetime.fromtimestamp(f.stat().st_mtime) < desde:
                continue
            lineas = f.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lineas:
            if '"usage"' not in line:
                continue
            try:
                e = json.loads(line)
                m = e["message"]
                u, modelo = m["usage"], m.get("model", "")
                ts = datetime.fromisoformat(e["timestamp"].replace("Z", "+00:00")).astimezone().replace(tzinfo=None)
            except (ValueError, KeyError, TypeError, AttributeError):
                continue
            if ts < desde or not modelo or modelo.startswith("<"):
                continue
            cc = u.get("cache_creation") or {}
            escritura_1h = cc.get("ephemeral_1h_input_tokens", 0)
            escritura = u.get("cache_creation_input_tokens", 0)
            por_id[m.get("id") or f"{f}:{len(por_id)}"] = {
                "ts": ts, "modelo": modelo, "sesion": str(e.get("sessionId") or f.stem), "cwd": e.get("cwd", ""),
                "tokens": {"input": u.get("input_tokens", 0), "cache_read": u.get("cache_read_input_tokens", 0),
                           "cache_write_5m": max(0, escritura - escritura_1h), "cache_write_1h": escritura_1h,
                           "output": u.get("output_tokens", 0)},
            }
    return sorted(por_id.values(), key=lambda r: r["ts"])


def _titulos_claude() -> dict[str, str]:
    """Título de cada sesión de Claude Code (el último que le puso la app), por sessionId."""
    titulos: dict[str, str] = {}
    for f in CLAUDE_PROJECTS_DIR.rglob("*.jsonl"):
        try:
            lineas = f.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lineas:
            if '"custom-title"' not in line:
                continue
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("type") == "custom-title" and e.get("customTitle"):
                titulos[str(e.get("sessionId") or f.stem)] = e["customTitle"]
    return titulos


def sesiones_claude(dias: int = PANEL_DIAS) -> dict:
    """Desglose de Claude por sesión: datos fijos (título, carpeta) y consumo por día para filtrar por fechas."""
    titulos, info, por_dia = _titulos_claude(), {}, {}
    for r in _mensajes_claude(datetime.now() - timedelta(days=dias)):
        sid, t = r["sesion"], r["tokens"]
        i = info.setdefault(sid, {"titulo": titulos.get(sid, ""), "carpeta": r["cwd"]})
        i["carpeta"] = i["carpeta"] or r["cwd"]
        k = (sid, r["ts"].date().isoformat())
        d = por_dia.setdefault(k, {"sesion": sid, "dia": k[1], "respuestas": 0, "tokens_entrada": 0, "tokens_cache": 0,
                                   "tokens_salida": 0, "costo_equivalente_usd": 0.0})
        d["respuestas"] += 1
        d["tokens_entrada"] += t["input"] + t["cache_write_5m"] + t["cache_write_1h"]
        d["tokens_cache"] += t["cache_read"]
        d["tokens_salida"] += t["output"]
        d["costo_equivalente_usd"] += costo_claude(r["modelo"], t, r["ts"].isoformat())
    for d in por_dia.values():
        d["costo_equivalente_usd"] = round(d["costo_equivalente_usd"], 4)
    return {"info": info, "por_dia": sorted(por_dia.values(), key=lambda d: d["dia"])}


CREADOR_PEDIDO: dict[str, str] = {}  # pedido_id -> sesión de Claude que lo creó (lo llena _pedidos_por_sesion)


def _pedidos_por_sesion() -> dict[str, set]:
    """Pedidos que usó cada sesión de Claude Code: los pedido_id que pasó a las herramientas del orquestador y los
    que creó con iniciar_pedido. Así su trabajo se cuenta en sus pedidos y no en el de otra sesión."""
    por: dict[str, set] = {}
    for f in CLAUDE_PROJECTS_DIR.rglob("*.jsonl"):
        iniciados: set = set()
        try:
            lineas = f.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lineas:
            if "mcp__orchestrator__" not in line and "tool_use_id" not in line:
                continue
            try:
                e = json.loads(line)
                contenido = (e.get("message") or {}).get("content")
            except (ValueError, AttributeError):
                continue
            if not isinstance(contenido, list):
                continue
            sid = str(e.get("sessionId") or f.stem)
            for b in contenido:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "tool_use" and str(b.get("name", "")).startswith("mcp__orchestrator__"):
                    pid = (b.get("input") or {}).get("pedido_id")
                    if pid:
                        por.setdefault(sid, set()).add(pid)
                    if b["name"].endswith("iniciar_pedido"):
                        iniciados.add(b.get("id"))
                elif b.get("type") == "tool_result" and b.get("tool_use_id") in iniciados:
                    texto = json.dumps(b.get("content"), ensure_ascii=False)
                    nuevos = re.findall(r"\d{8}-\d{6}-[a-z0-9-]{3,}", texto)
                    por.setdefault(sid, set()).update(nuevos)
                    for pid in nuevos:
                        CREADOR_PEDIDO.setdefault(pid, sid)
    return por


def costo_claude(modelo: str, tokens: dict, fecha: str = "") -> float:
    """Equivalente por la API de Anthropic: escribir caché cuesta 1,25× la entrada (5 min y 1 h: así cuadra con
    el informe de uso de Claude Code)."""
    precio = _precio_vigente(modelo, fecha)
    if not precio:
        return 0.0
    escritura = (tokens.get("cache_write_5m", 0) + tokens.get("cache_write_1h", 0)) * 1.25
    return costo_innti(modelo, tokens, fecha) + escritura * precio[0] / 1_000_000


def consumo_claude(dias: int = PANEL_DIAS) -> list[dict]:
    """Consumo de Claude por respuesta: ts, modelo, sesión, tokens (entrada incluye caché) y equivalente API."""
    filas = []
    for r in _mensajes_claude(datetime.now() - timedelta(days=dias)):
        t = r["tokens"]
        filas.append({"ts": r["ts"], "modelo": r["modelo"], "sesion": r["sesion"], "cwd": r.get("cwd", ""),
                      "entrada": t["input"] + t["cache_read"] + t["cache_write_5m"] + t["cache_write_1h"],
                      "cache": t["cache_read"], "salida": t["output"], "equivalente": costo_claude(r["modelo"], t, r["ts"].isoformat())})
    return filas


def _pedido_de(ts: datetime, ventanas: list[tuple]) -> str:
    """El pedido activo en ese momento (el más reciente si se solapan): ahí se cuenta el trabajo de Claude."""
    activos = [(ini, pid) for ini, fin, pid in ventanas if ini <= ts <= fin]
    return max(activos)[1] if activos else ""


def _sueltas(pedidos: list[dict]) -> tuple[dict, dict]:
    """Delegaciones registradas sin pedido (p. ej. seguimientos): cada una se asigna al pedido más reciente del
    mismo proyecto creado antes de ella (prefiriendo los abiertos). Devuelve ({pedido_id: extra}, {ruta: extra})
    con lo asignado y lo que no encontró pedido."""
    def norm(r):
        return os.path.normcase(os.path.normpath(r)) if r else ""

    def mismo(a, b):
        return bool(a and b) and (a == b or a.startswith(b + os.sep) or b.startswith(a + os.sep))

    asignadas: dict[str, dict] = {}
    huerfanas: dict[str, dict] = {}
    if not COSTOS_FILE.exists():
        return asignadas, huerfanas
    for line in COSTOS_FILE.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get("pedido_id"):
            continue
        ruta, ts = norm(r.get("ruta", "")), r.get("ts", "")
        cand = [p for p in pedidos if mismo(ruta, norm(p.get("ruta", ""))) and (p.get("creado") or "") <= ts]
        cand.sort(key=lambda p: (p["estado"] == "abierto", p.get("creado") or ""), reverse=True)
        destino = asignadas.setdefault(cand[0]["id"], {}) if cand else huerfanas.setdefault(r.get("ruta", ""), {})
        t = r.get("tokens") or {}
        for k, v in (("n", 1), ("tokens", t.get("input", 0) + t.get("cache_read", 0) + t.get("output", 0)
                                 + t.get("reasoning", 0)),
                     ("costo_usd", r.get("costo_usd", 0)), ("costo_equivalente_usd", r.get("costo_equivalente_usd", 0))):
            destino[k] = destino.get(k, 0) + v
        g = destino.setdefault("agentes", {}).setdefault(r.get("agente", "?"),
                                                        {"tokens": 0, "costo_usd": 0.0, "costo_equivalente_usd": 0.0,
                                                         "modelos": []})
        if r.get("modelo") and r["modelo"] not in g["modelos"]:
            g["modelos"].append(r["modelo"])
        g["tokens"] += t.get("input", 0) + t.get("cache_read", 0) + t.get("output", 0) + t.get("reasoning", 0)
        g["costo_usd"] += r.get("costo_usd", 0)
        g["costo_equivalente_usd"] += r.get("costo_equivalente_usd", 0)
        destino["desde"] = min(destino.get("desde", ts), ts)
        destino["hasta"] = max(destino.get("hasta", ts), ts)
    return asignadas, huerfanas


def datos_panel() -> dict:
    """Pedidos de los últimos PANEL_DIAS días (con fechas, tokens y gasto) y costos diarios, para el panel."""
    desde = (datetime.now() - timedelta(days=PANEL_DIAS)).isoformat(timespec="seconds")
    consumo = consumo_por_item()
    vacio = {"tokens_entrada": 0, "tokens_salida": 0, "costo_usd": 0.0, "costo_equivalente_usd": 0.0}
    pedidos = [p for f in sorted(TAREAS_DIR.glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True)
               if (p := _load(f.stem)) and p.get("actualizado", "") >= desde][:PANEL_MAX_PEDIDOS]

    # Claude: por día y modelo, y cada respuesta asignada al pedido que estaba activo en ese momento.
    ahora = datetime.now()
    ventanas = []
    for p in pedidos:
        try:
            ini = datetime.fromisoformat(p.get("creado") or p["actualizado"])
            fin = datetime.fromisoformat(p["actualizado"]) if p["estado"] != "abierto" else ahora
            ventanas.append((ini, fin, p["id"]))
        except ValueError:
            continue
    claude_dia: dict[tuple, dict] = {}
    claude_pedido: dict[str, dict] = {}
    claude_resp: dict[str, list] = {}
    sesiones_sin_pedido: dict[str, list] = {}
    ses_agg: dict[str, dict] = {}
    por_sesion = _pedidos_por_sesion()
    for r in consumo_claude():
        k = (r["ts"].date().isoformat(), r["modelo"])
        a = claude_dia.setdefault(k, {"dia": k[0], "agente": "claude", "modelo": k[1], "tareas": 0,
                                      "tokens_entrada": 0, "tokens_cache": 0, "tokens_salida": 0, "costo_usd": 0.0,
                                      "costo_equivalente_usd": 0.0, "sesiones": []})
        a["tareas"] += 1  # respuestas del modelo; el panel muestra las sesiones distintas
        if r["sesion"][:12] not in a["sesiones"]:
            a["sesiones"].append(r["sesion"][:12])
        a["tokens_entrada"] += r["entrada"]
        a["tokens_cache"] += r["cache"]
        a["tokens_salida"] += r["salida"]
        a["costo_equivalente_usd"] += r["equivalente"]
        sa = ses_agg.setdefault(r["sesion"], {"inicio": r["ts"], "fin": r["ts"], "tokens": 0, "eq": 0.0,
                                              "modelos": set(), "cwd": r.get("cwd", ""), "fuera_tokens": 0, "fuera_eq": 0.0})
        sa["inicio"], sa["fin"] = min(sa["inicio"], r["ts"]), max(sa["fin"], r["ts"])
        sa["tokens"] += r["entrada"] + r["salida"]
        sa["eq"] += r["equivalente"]
        sa["modelos"].add(r["modelo"])
        sa["cwd"] = sa["cwd"] or r.get("cwd", "")
        propios = [v for v in ventanas if v[2] in por_sesion.get(r["sesion"], ())]
        # Solo cuenta en un pedido de su propia sesión que estuviera activo en ese momento; lo demás es trabajo
        # de la sesión sin pedido (no se le carga a pedidos de otras sesiones).
        pid = _pedido_de(r["ts"], propios) if propios else ""
        if not pid:
            sesiones_sin_pedido.setdefault(r["sesion"], []).append(r)
        if pid:
            c = claude_pedido.setdefault(pid, {"tokens": 0, "costo_equivalente_usd": 0.0})
            c["tokens"] += r["entrada"] + r["salida"]
            c["costo_equivalente_usd"] += r["equivalente"]
            claude_resp.setdefault(pid, []).append(r)
    for a in claude_dia.values():
        a["costo_equivalente_usd"] = round(a["costo_equivalente_usd"], 5)

    sueltas, huerfanas = _sueltas(pedidos)
    filas = []
    for p in pedidos:
        extra = sueltas.get(p["id"], {})
        # Trabajo de Claude repartido por ítem según el momento de cada respuesta (los seguimientos sin ítem
        # forman un ítem virtual al final, con su propia ventana).
        vent = _ventanas_items(p, consumo)
        n_extra = len(p["items"]) + 1
        if extra.get("n"):
            try:
                vent.append((datetime.fromisoformat(extra["desde"]), datetime.fromisoformat(extra["hasta"]), n_extra))
            except (KeyError, ValueError):
                pass
        claude_item: dict[int, dict] = {}
        for r in claude_resp.get(p["id"], []):
            destino = _items_de(r["ts"], vent) or [1]
            for n in destino:
                ci = claude_item.setdefault(n, {"tokens": 0, "costo_usd": 0.0, "costo_equivalente_usd": 0.0,
                                                "modelos": []})
                if r["modelo"] not in ci["modelos"]:
                    ci["modelos"].append(r["modelo"])
                ci["tokens"] += (r["entrada"] + r["salida"]) / len(destino)
                ci["costo_equivalente_usd"] += r["equivalente"] / len(destino)

        def desglose(agentes: dict, n: int) -> dict:
            out = {}
            for nombre, g in (agentes or {}).items():
                clave = "innti" if nombre == "opencode" else "agy" if nombre == "antigravity" else nombre
                out[clave] = {k: (round(v, 4) if isinstance(v, float) else v) for k, v in g.items()}
            if n in claude_item:
                out["claude"] = {k: (round(v) if k == "tokens" else round(v, 4) if isinstance(v, float) else v)
                                 for k, v in claude_item[n].items()}
            return out

        items = []
        for n, i in enumerate(p["items"], 1):
            c = consumo.get((p["id"], n), vacio)
            ag = desglose(c.get("agentes"), n)
            items.append({"texto": i["texto"], "estado": i["estado"], "agente": i.get("agente", ""),
                          "modelo": i.get("modelo", ""), "rondas": i.get("rondas", 0),
                          "costo_usd": round(c["costo_usd"] or i.get("costo_usd", 0), 4),
                          "costo_equivalente_usd": round(c["costo_equivalente_usd"], 4),
                          "tokens": c["tokens_entrada"] + c["tokens_salida"], "agentes": ag})
        if 0 in claude_item:
            items.append({"texto": "Trabajo de Claude sin ítem (después del último ajuste)", "estado": "hecho",
                          "virtual": True, "agente": "", "modelo": "", "rondas": 0, "costo_usd": 0.0,
                          "costo_equivalente_usd": 0.0, "tokens": 0, "agentes": desglose({}, 0)})
        if extra.get("n") or n_extra in claude_item:
            items.append({"texto": f"Seguimientos sin ítem ({extra.get('n', 0)} delegaciones)", "estado": "hecho",
                          "virtual": True, "agente": "", "modelo": "", "rondas": 0,
                          "costo_usd": round(extra.get("costo_usd", 0), 4),
                          "costo_equivalente_usd": round(extra.get("costo_equivalente_usd", 0), 4),
                          "tokens": extra.get("tokens", 0), "agentes": desglose(extra.get("agentes"), n_extra)})
        filas.append({
            "id": p["id"], "titulo": p["titulo"], "ruta": p.get("ruta", ""), "estado": p["estado"],
            "creado": p.get("creado", ""), "actualizado": max(p["actualizado"], extra.get("hasta", "")),
            "costo_usd": round(sum(i["costo_usd"] for i in items), 4),
            "costo_equivalente_usd": round(sum(i["costo_equivalente_usd"] for i in items), 4),
            "tokens": sum(i["tokens"] for i in items),
            "claude": {k: round(v, 4) for k, v in claude_pedido.get(p["id"], {"tokens": 0,
                                                                              "costo_equivalente_usd": 0.0}).items()},
            "items": items,
        })
    # Lo que no encontró pedido va en un pedido virtual por proyecto, para que los totales cuadren.
    for ruta, extra in huerfanas.items():
        if extra.get("hasta", "") < desde:
            continue
        filas.append({
            "id": "sin-pedido-" + _slug(ruta or "general"), "titulo": "Delegaciones sin pedido", "ruta": ruta,
            "estado": "cerrado", "creado": extra["desde"], "actualizado": extra["hasta"],
            "costo_usd": round(extra.get("costo_usd", 0), 4),
            "costo_equivalente_usd": round(extra.get("costo_equivalente_usd", 0), 4), "tokens": extra.get("tokens", 0),
            "claude": {"tokens": 0, "costo_equivalente_usd": 0.0},
            "items": [{"texto": f"{extra.get('n', 0)} delegaciones sin pedido", "estado": "hecho", "virtual": True,
                       "agente": "", "modelo": "", "rondas": 0, "costo_usd": round(extra.get("costo_usd", 0), 4),
                       "costo_equivalente_usd": round(extra.get("costo_equivalente_usd", 0), 4),
                       "tokens": extra.get("tokens", 0),
                       "agentes": {("innti" if k == "opencode" else "agy" if k == "antigravity" else k):
                                   {kk: (round(vv, 4) if isinstance(vv, float) else vv) for kk, vv in g.items()}
                                   for k, g in extra.get("agentes", {}).items()}}]})
    # Sesiones: cada pedido va con la sesión de Claude que lo creó (o la primera que lo usó); los que no tienen
    # sesión conocida (delegaciones sueltas, pedidos de otra máquina) van en "Sin sesión de Claude".
    for sid, rs in sesiones_sin_pedido.items():
        ses_agg[sid]["fuera_tokens"] = sum(r["entrada"] + r["salida"] for r in rs)
        ses_agg[sid]["fuera_eq"] = sum(r["equivalente"] for r in rs)
    orden_ses = sorted(ses_agg, key=lambda sid: ses_agg[sid]["inicio"])
    duenio = {}
    for sid in orden_ses:
        for pid in por_sesion.get(sid, ()):
            duenio.setdefault(pid, sid)
    duenio.update({pid: sid for pid, sid in CREADOR_PEDIDO.items() if sid in ses_agg})
    titulos = _titulos_claude()
    sesiones = {}
    for f in filas:
        sid = duenio.get(f["id"], "sin-sesion")
        f["sesion"] = sid
        sesiones.setdefault(sid, []).append(f["id"])
    lista_ses = []
    for sid in set(ses_agg) | set(sesiones):
        a = ses_agg.get(sid)
        pedidos_s = [f for f in filas if f.get("sesion") == sid]
        ini = a["inicio"].isoformat(timespec="seconds") if a else min((f["creado"] for f in pedidos_s), default="")
        fin = a["fin"].isoformat(timespec="seconds") if a else max((f["actualizado"] for f in pedidos_s), default="")
        fin = max([fin] + [f["actualizado"] for f in pedidos_s])
        if fin < desde:
            continue
        lista_ses.append({
            "id": sid, "titulo": (titulos.get(sid) if a else "") or ("Sin sesión de Claude" if sid == "sin-sesion"
                                                                    else f"Sesión {sid[:8]}"),
            "ruta": (a or {}).get("cwd", "") or next((f["ruta"] for f in pedidos_s if f.get("ruta")), ""),
            "inicio": ini, "fin": fin, "pedidos": sesiones.get(sid, []),
            "claude": {"tokens": round((a or {}).get("tokens", 0)), "costo_equivalente_usd": round((a or {}).get("eq", 0.0), 4),
                       "modelos": sorted((a or {}).get("modelos", set()))},
            "claude_fuera": {"tokens": round((a or {}).get("fuera_tokens", 0)),
                             "costo_equivalente_usd": round((a or {}).get("fuera_eq", 0.0), 4)}})
    lista_ses.sort(key=lambda x: x["fin"], reverse=True)
    filas.sort(key=lambda f: f["actualizado"], reverse=True)
    diarios = sorted(costos_diarios() + list(claude_dia.values()), key=lambda a: a["dia"])
    data = {"pedidos": filas, "sesiones": lista_ses, "costos_diarios": diarios, "dias": PANEL_DIAS,
            "generado": _now()}
    # Un documento del panel admite 256 KiB: si se pasa, recorta los pedidos más antiguos.
    while len(json.dumps(data, ensure_ascii=False).encode("utf-8")) > 240_000 and data["pedidos"]:
        data["pedidos"].pop()
    return data


# ----------------------------------------------------------------------------- aprendizajes por proyecto

def proyecto_de(ruta: str) -> str:
    """Nombre del proyecto: primera carpeta bajo workspaces/personal/universidad."""
    p = Path(ruta)
    home = Path.home()
    for base in (Path(os.getenv("TRABAJO_DIR") or home / "workspaces"), Path(os.getenv("PERSONAL_DIR") or home / "personal"),
                 Path(os.getenv("UNIVERSIDAD_DIR") or home / "universidad")):
        try:
            rel = p.resolve().relative_to(base.resolve())
            return rel.parts[0] if rel.parts else base.name
        except ValueError:
            continue
    return p.name or str(p)


def registrar_aprendizaje(ruta_o_proyecto: str, agente: str, modelo: str, resultado: str, nota: str) -> str:
    """resultado: 'bien' | 'regular' | 'mal'."""
    APRENDIZAJES_FILE.parent.mkdir(parents=True, exist_ok=True)
    proyecto = proyecto_de(ruta_o_proyecto) if ("\\" in ruta_o_proyecto or "/" in ruta_o_proyecto) else ruta_o_proyecto
    with open(APRENDIZAJES_FILE, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": _now(), "proyecto": proyecto, "agente": agente, "modelo": modelo,
                             "resultado": resultado, "nota": nota}, ensure_ascii=False) + "\n")
    return f"Aprendizaje guardado para {proyecto}: {modelo} → {resultado}."


def consultar_aprendizajes(ruta_o_proyecto: str = "", limite: int = 12) -> str:
    if not APRENDIZAJES_FILE.exists():
        return "Sin aprendizajes registrados todavía."
    proyecto = ""
    if ruta_o_proyecto:
        proyecto = proyecto_de(ruta_o_proyecto) if ("\\" in ruta_o_proyecto or "/" in ruta_o_proyecto) else ruta_o_proyecto
    filas = []
    for line in APRENDIZAJES_FILE.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if not proyecto or r["proyecto"].lower() == proyecto.lower():
            filas.append(r)
    if not filas:
        return f"Sin aprendizajes para {proyecto or 'ningún proyecto'}."
    icon = {"bien": "👍", "regular": "➖", "mal": "👎"}
    out = [f"Aprendizajes{' de ' + proyecto if proyecto else ''} (más recientes primero):"]
    for r in reversed(filas[-limite:]):
        out.append(f"- {icon.get(r['resultado'], '•')} {r['proyecto']} · {r['agente']} · {r['modelo']}: {r['nota']}")
    return "\n".join(out)
