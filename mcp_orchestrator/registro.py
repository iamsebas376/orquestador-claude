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
                    ruta: str = "", equivalente: float = 0.0) -> None:
    """costo_usd = gasto real (cuenta para el presupuesto). costo_equivalente_usd = lo que costaría por la API
    un agente sin cobro por token (Antigravity con plan Pro); se informa aparte y no frena nada."""
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    fila = {"ts": _now(), "agente": agente, "modelo": modelo, "tokens": tokens, "costo_usd": round(costo, 6),
            "pedido_id": pedido_id, "item": item, "ruta": ruta}
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


def iniciar_pedido(titulo: str, items: list[str], contexto: str = "") -> str:
    """Crea el checklist de un pedido del usuario. Devuelve el pedido_id para vincular delegaciones."""
    pid = f"{datetime.now():%Y%m%d-%H%M%S}-{_slug(titulo, 30)}"
    p = {"id": pid, "titulo": titulo, "contexto": contexto, "estado": "abierto", "creado": _now(),
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


def vincular_delegacion(pedido_id: str, item: int, agente: str, modelo: str, costo: float, resultado: str) -> str:
    """Tras una delegación: pasa el ítem a 'revisando', suma costo y deja nota. Devuelve aviso para Claude."""
    if not pedido_id or not item:
        return ""
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
                                   "tokens_entrada": 0, "tokens_salida": 0, "costo_usd": 0.0,
                                   "costo_equivalente_usd": 0.0})
            a["tareas"] += 1
            a["tokens_entrada"] += t.get("input", 0) + t.get("cache_read", 0)
            a["tokens_salida"] += t.get("output", 0) + t.get("reasoning", 0)
            a["costo_usd"] += r.get("costo_usd", 0)
            a["costo_equivalente_usd"] += r.get("costo_equivalente_usd", 0)
    filas = sorted(agg.values(), key=lambda a: a["dia"])
    for a in filas:
        a["costo_usd"] = round(a["costo_usd"], 5)
        a["costo_equivalente_usd"] = round(a["costo_equivalente_usd"], 5)
    return filas


def datos_panel() -> dict:
    """Pedidos de los últimos PANEL_DIAS días (con fechas) y costos diarios, para el panel de control."""
    desde = (datetime.now() - timedelta(days=PANEL_DIAS)).isoformat(timespec="seconds")
    filas = []
    for f in sorted(TAREAS_DIR.glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True):
        p = _load(f.stem)
        if not p or p.get("actualizado", "") < desde:
            continue
        if len(filas) >= PANEL_MAX_PEDIDOS:
            break
        filas.append({
            "id": p["id"], "titulo": p["titulo"], "estado": p["estado"], "creado": p.get("creado", ""),
            "actualizado": p["actualizado"],
            "costo_usd": round(sum(i.get("costo_usd", 0) for i in p["items"]), 4),
            "items": [{"texto": i["texto"], "estado": i["estado"], "agente": i.get("agente", ""),
                       "modelo": i.get("modelo", ""), "rondas": i.get("rondas", 0),
                       "costo_usd": round(i.get("costo_usd", 0), 4)} for i in p["items"]],
        })
    data = {"pedidos": filas, "costos_diarios": costos_diarios(), "dias": PANEL_DIAS, "generado": _now()}
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
