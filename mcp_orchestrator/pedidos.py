"""Pedidos (checklists por ítem) y aprendizajes por proyecto."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path

import registro as R
from registro import ESTADOS, _now, _slug, _usd


def _progreso(p: dict) -> str:
    hechos = sum(1 for i in p["items"] if i["estado"] in ("hecho", "omitido"))
    return f"{hechos}/{len(p['items'])}"


def _lista(p: dict, desde: int = 1) -> str:
    return "\n".join(f"{n}. {i['texto']}" for n, i in enumerate(p["items"], 1) if n >= desde)


def _path(pid: str) -> Path:
    return R.TAREAS_DIR / f"{pid}.json"


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
    R.TAREAS_DIR.mkdir(parents=True, exist_ok=True)
    p["actualizado"] = _now()
    _path(p["id"]).write_text(json.dumps(p, ensure_ascii=False, indent=2), encoding="utf-8")
    (R.TAREAS_DIR / f"{p['id']}.md").write_text(_render(p), encoding="utf-8")


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
    return f"pedido_id: {pid}\nChecklist: {R.TAREAS_DIR / (pid + '.md')}\n{_lista(p)}"


def agregar_items(pedido_id: str, items: list[str]) -> str:
    p = _load(pedido_id)
    if not p:
        return f"Error: no existe el pedido {pedido_id}."
    primero = len(p["items"]) + 1
    p["items"] += [{"texto": t, "estado": "pendiente", "rondas": 0, "costo_usd": 0.0, "notas": []} for t in items]
    _save(p)
    return f"Agregados al pedido {pedido_id} ({_progreso(p)}):\n{_lista(p, primero)}"


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
        if it["rondas"] > R.MAX_RONDAS:
            aviso = (f"\n⚠ Límite alcanzado: el ítem {item} ya lleva {it['rondas']} rondas de corrección "
                     f"(máximo {R.MAX_RONDAS}). Detente y consulta al usuario antes de volver a delegar.")
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
    return f"Ítem {item} → {ESTADOS[estado]} {estado} · pedido {pedido_id} {_progreso(p)}{aviso}"


def cerrar_pedido(pedido_id: str, resumen: str, estado: str = "cerrado") -> str:
    p = _load(pedido_id)
    if not p:
        return f"Error: no existe el pedido {pedido_id}."
    p["estado"], p["resumen"] = estado, resumen
    _save(p)
    return f"Pedido {pedido_id} {estado} ({_progreso(p)})."


def ver_pedido(pedido_id: str = "") -> str:
    if not pedido_id:
        files = sorted(R.TAREAS_DIR.glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True)
        if not files:
            return "No hay pedidos registrados."
        pedido_id = files[0].stem
    p = _load(pedido_id)
    return _render(p) if p else f"Error: no existe el pedido {pedido_id}."


def listar_pedidos(solo_abiertos: bool = True, limite: int = 15) -> str:
    files = sorted(R.TAREAS_DIR.glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True)
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
    cand = [p for f in R.TAREAS_DIR.glob("*.json") if (p := _load(f.stem)) and mismo(norm(ruta), norm(p.get("ruta", "")))]
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
    R.APRENDIZAJES_FILE.parent.mkdir(parents=True, exist_ok=True)
    proyecto = proyecto_de(ruta_o_proyecto) if ("\\" in ruta_o_proyecto or "/" in ruta_o_proyecto) else ruta_o_proyecto
    with open(R.APRENDIZAJES_FILE, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": _now(), "proyecto": proyecto, "agente": agente, "modelo": modelo,
                             "resultado": resultado, "nota": nota}, ensure_ascii=False) + "\n")
    return f"Aprendizaje guardado para {proyecto}: {modelo} → {resultado}."


def consultar_aprendizajes(ruta_o_proyecto: str = "", limite: int = 12) -> str:
    if not R.APRENDIZAJES_FILE.exists():
        return "Sin aprendizajes registrados todavía."
    proyecto = ""
    if ruta_o_proyecto:
        proyecto = proyecto_de(ruta_o_proyecto) if ("\\" in ruta_o_proyecto or "/" in ruta_o_proyecto) else ruta_o_proyecto
    filas = []
    for line in R.APRENDIZAJES_FILE.read_text(encoding="utf-8").splitlines():
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
