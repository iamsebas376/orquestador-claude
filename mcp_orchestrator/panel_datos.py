"""Datos del panel: consumo de Claude leído de sus transcripts y el JSON que consume config/panel.html."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta

import registro as R
from costos import consumo_por_item, costo_claude, costos_diarios
from pedidos import _load
from registro import _now


_CACHE: dict[str, dict] = {}  # transcript -> lo ya leído; los .jsonl solo crecen, así que se lee desde donde se quedó
_PEDIDO_EN_TEXTO = re.compile(r"\d{8}-\d{6}-[a-z0-9-]{3,}")


def _procesar(c: dict, f, line: str) -> None:
    """Una línea de un transcript: respuestas de Claude con su uso, títulos de sesión y pedidos del orquestador."""
    uso, titulo = '"usage"' in line, '"custom-title"' in line
    orq = "mcp__orchestrator__" in line or "tool_use_id" in line
    if not (uso or titulo or orq):
        return
    try:
        e = json.loads(line)
    except ValueError:
        return
    if not isinstance(e, dict):
        return
    sid = str(e.get("sessionId") or f.stem)
    if titulo and e.get("type") == "custom-title" and e.get("customTitle"):
        c["titulos"][sid] = e["customTitle"]
    m = e.get("message")
    if not isinstance(m, dict):
        return
    if uso:
        try:
            u, modelo = m["usage"], m.get("model", "")
            ts = datetime.fromisoformat(e["timestamp"].replace("Z", "+00:00")).astimezone().replace(tzinfo=None)
        except (ValueError, KeyError, TypeError, AttributeError):
            u = None
        if u is not None and modelo and not modelo.startswith("<"):
            cc = u.get("cache_creation") or {}
            escritura_1h = cc.get("ephemeral_1h_input_tokens", 0)
            escritura = u.get("cache_creation_input_tokens", 0)
            clave = m.get("id") or f"{f}:{len(c['msgs'])}"
            c["msgs"][clave] = {
                "ts": ts, "modelo": modelo, "sesion": sid, "cwd": e.get("cwd", ""),
                "tokens": {"input": u.get("input_tokens", 0), "cache_read": u.get("cache_read_input_tokens", 0),
                           "cache_write_5m": max(0, escritura - escritura_1h), "cache_write_1h": escritura_1h,
                           "output": u.get("output_tokens", 0)},
            }
    contenido = m.get("content")
    if not orq or not isinstance(contenido, list):
        return
    for b in contenido:
        if not isinstance(b, dict):
            continue
        if b.get("type") == "tool_use" and str(b.get("name", "")).startswith("mcp__orchestrator__"):
            pid = (b.get("input") or {}).get("pedido_id")
            if pid:
                c["pedidos"].setdefault(sid, set()).add(pid)
            if b["name"].endswith("iniciar_pedido"):
                c["iniciados"].add(b.get("id"))
            elif b["name"].endswith(("ejecutar_trabajo_opencode_innti", "ejecutar_personal_antigravity")):
                c["delegados"].add(b.get("id"))
        elif b.get("type") == "tool_result" and b.get("tool_use_id") in (c["iniciados"] | c["delegados"]):
            nuevos = _PEDIDO_EN_TEXTO.findall(json.dumps(b.get("content"), ensure_ascii=False))
            c["pedidos"].setdefault(sid, set()).update(nuevos)
            if b.get("tool_use_id") in c["iniciados"]:
                for pid in nuevos:
                    c["creador"].setdefault(pid, sid)


def _leer(f) -> dict | None:
    """Estado de un transcript, leyendo solo lo que se añadió desde la última vez."""
    try:
        st = f.stat()
        c = _CACHE.get(str(f))
        if c and c["size"] == st.st_size and c["mtime"] == st.st_mtime_ns:
            return c
        if not c or st.st_size < c["size"]:  # nuevo o reescrito
            c = _CACHE[str(f)] = {"pos": 0, "size": 0, "mtime": 0, "msgs": {}, "titulos": {}, "pedidos": {},
                                  "creador": {}, "iniciados": set(), "delegados": set()}
        with open(f, "rb") as fh:
            fh.seek(c["pos"])
            datos = fh.read()
    except OSError:
        return None
    fin = datos.rfind(b"\n") + 1  # una línea a medio escribir se lee en la próxima vuelta
    resto = datos[fin:]
    if resto.strip():
        try:
            json.loads(resto)
            fin = len(datos)  # última línea completa aunque no termine en salto de línea
        except ValueError:
            pass
    for line in datos[:fin].decode("utf-8", errors="replace").splitlines():
        _procesar(c, f, line)
    c["pos"] += fin
    c["size"], c["mtime"] = st.st_size, st.st_mtime_ns
    return c


def _escanear(desde: datetime) -> dict:
    """Transcripts de Claude Code (~/.claude/projects/**/*.jsonl, subagentes incluidos) modificados desde `desde`."""
    por_id: dict = {}
    titulos: dict = {}
    pedidos: dict = {}
    creador: dict = {}
    for f in R.CLAUDE_PROJECTS_DIR.rglob("*.jsonl"):
        try:
            if datetime.fromtimestamp(f.stat().st_mtime) < desde:
                continue
        except OSError:
            continue
        c = _leer(f)
        if not c:
            continue
        por_id.update(c["msgs"])
        titulos.update(c["titulos"])
        for sid, s in c["pedidos"].items():
            pedidos.setdefault(sid, set()).update(s)
        for pid, sid in c["creador"].items():
            creador.setdefault(pid, sid)
    return {"msgs": por_id, "titulos": titulos, "pedidos": pedidos, "creador": creador}


def _mensajes_claude(desde: datetime) -> list[dict]:
    """Respuestas de Claude Code desde `desde` (hora local); cada respuesta se repite por bloque: una por id."""
    return sorted((m for m in _escanear(desde)["msgs"].values() if m["ts"] >= desde), key=lambda r: r["ts"])


def _titulos_claude() -> dict[str, str]:
    """Título de cada sesión de Claude Code (el último que le puso la app), por sessionId."""
    return _escanear(datetime.min)["titulos"]


CREADOR_PEDIDO: dict[str, str] = {}  # pedido_id -> sesión de Claude que lo creó (lo llena _pedidos_por_sesion)


def _pedidos_por_sesion() -> dict[str, set]:
    """Pedidos que usó cada sesión de Claude Code: los pedido_id que pasó a las herramientas del orquestador y los
    que creó con iniciar_pedido. Así su trabajo se cuenta en sus pedidos y no en el de otra sesión."""
    r = _escanear(datetime.min)
    CREADOR_PEDIDO.clear()
    CREADOR_PEDIDO.update(r["creador"])
    return r["pedidos"]


def consumo_claude(dias: int = R.PANEL_DIAS) -> list[dict]:
    """Consumo de Claude por respuesta: ts, modelo, sesión, tokens (entrada incluye caché) y equivalente API."""
    filas = []
    for r in _mensajes_claude(datetime.now() - timedelta(days=dias)):
        t = r["tokens"]
        filas.append({"ts": r["ts"], "modelo": r["modelo"], "sesion": r["sesion"], "cwd": r.get("cwd", ""),
                      "entrada": t["input"] + t["cache_read"] + t["cache_write_5m"] + t["cache_write_1h"],
                      "cache": t["cache_read"], "salida": t["output"],
                      "equivalente": costo_claude(r["modelo"], t, r["ts"].isoformat())})
    return filas


def _pedido_de(ts: datetime, ventanas: list[tuple]) -> str:
    """El pedido activo en ese momento (el más reciente si se solapan): ahí se cuenta el trabajo de Claude."""
    activos = [(ini, pid) for ini, fin, pid in ventanas if ini <= ts <= fin]
    return max(activos)[1] if activos else ""


def datos_panel() -> dict:
    """Pedidos de los últimos R.PANEL_DIAS días (con fechas, tokens y gasto) y costos diarios, para el panel."""
    desde = (datetime.now() - timedelta(days=R.PANEL_DIAS)).isoformat(timespec="seconds")
    consumo = consumo_por_item()
    vacio = {"tokens_entrada": 0, "tokens_salida": 0, "costo_usd": 0.0, "costo_equivalente_usd": 0.0}
    pedidos = [p for f in sorted(R.TAREAS_DIR.glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True)
               if (p := _load(f.stem)) and p.get("actualizado", "") >= desde][:R.PANEL_MAX_PEDIDOS]

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
        dd = sa.setdefault("por_dia", {}).setdefault(r["ts"].date().isoformat(),
                                                     {"tokens": 0, "eq": 0.0, "fuera_tokens": 0, "fuera_eq": 0.0})
        dd["tokens"] += r["entrada"] + r["salida"]
        dd["eq"] += r["equivalente"]
        if not pid:
            sa["fuera_tokens"] += r["entrada"] + r["salida"]
            sa["fuera_eq"] += r["equivalente"]
            dd["fuera_tokens"] += r["entrada"] + r["salida"]
            dd["fuera_eq"] += r["equivalente"]
        else:
            c = claude_pedido.setdefault(pid, {"tokens": 0, "costo_equivalente_usd": 0.0})
            c["tokens"] += r["entrada"] + r["salida"]
            c["costo_equivalente_usd"] += r["equivalente"]
    for a in claude_dia.values():
        a["costo_equivalente_usd"] = round(a["costo_equivalente_usd"], 5)

    def desglose(agentes: dict) -> dict:
        out = {}
        for nombre, g in (agentes or {}).items():
            clave = "innti" if nombre == "opencode" else "agy" if nombre == "antigravity" else nombre
            out[clave] = {k: (round(v, 4) if isinstance(v, float) else v) for k, v in g.items()}
        return out

    filas = []
    for p in pedidos:
        items = []
        for n, i in enumerate(p["items"], 1):
            c = consumo.get((p["id"], n), vacio)
            items.append({"texto": i["texto"], "estado": i["estado"], "agente": i.get("agente", ""),
                          "modelo": i.get("modelo", ""), "rondas": i.get("rondas", 0),
                          "costo_usd": round(c["costo_usd"] or i.get("costo_usd", 0), 4),
                          "costo_equivalente_usd": round(c["costo_equivalente_usd"], 4),
                          "tokens": c["tokens_entrada"] + c["tokens_salida"], "agentes": desglose(c.get("agentes"))})
        filas.append({
            "id": p["id"], "titulo": p["titulo"], "ruta": p.get("ruta", ""), "estado": p["estado"],
            "creado": p.get("creado", ""), "actualizado": p["actualizado"],
            "costo_usd": round(sum(i["costo_usd"] for i in items), 4),
            "costo_equivalente_usd": round(sum(i["costo_equivalente_usd"] for i in items), 4),
            "tokens": sum(i["tokens"] for i in items),
            "claude": {k: round(v, 4) for k, v in claude_pedido.get(p["id"], {"tokens": 0,
                                                                              "costo_equivalente_usd": 0.0}).items()},
            "items": items,
        })
    # Sesiones: cada pedido va con la sesión de Claude que lo creó (o la primera que lo usó); los que no tienen
    # sesión viva se omiten (su sesión se eliminó). Solo sin transcripts disponibles van en "Sin sesión de Claude".
    orden_ses = sorted(ses_agg, key=lambda sid: ses_agg[sid]["inicio"])
    duenio = {}
    for sid in orden_ses:
        for pid in por_sesion.get(sid, ()):
            duenio.setdefault(pid, sid)
    duenio.update({pid: sid for pid, sid in CREADOR_PEDIDO.items() if sid in ses_agg})
    if ses_agg:  # sin transcripts (otra máquina) no hay con qué comparar: se muestra todo
        filas = [f for f in filas if f["id"] in duenio]
    visibles = {f["id"] for f in filas} if ses_agg else None
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
                             "costo_equivalente_usd": round((a or {}).get("fuera_eq", 0.0), 4)},
            "por_dia": [{"dia": dia, "tokens": round(v["tokens"]), "costo_equivalente_usd": round(v["eq"], 4),
                         "fuera_tokens": round(v["fuera_tokens"]), "fuera_costo_equivalente_usd": round(v["fuera_eq"], 4)}
                        for dia, v in sorted((a or {}).get("por_dia", {}).items())]})
    lista_ses.sort(key=lambda x: x["fin"], reverse=True)
    filas.sort(key=lambda f: f["actualizado"], reverse=True)
    diarios = sorted(costos_diarios(pedidos=visibles) + list(claude_dia.values()), key=lambda a: a["dia"])
    data = {"pedidos": filas, "sesiones": lista_ses, "costos_diarios": diarios, "dias": R.PANEL_DIAS,
            "generado": _now()}
    # Un documento del panel admite 256 KiB: si se pasa, recorta los pedidos más antiguos.
    while len(json.dumps(data, ensure_ascii=False).encode("utf-8")) > 240_000 and data["pedidos"]:
        data["pedidos"].pop()
    return data
