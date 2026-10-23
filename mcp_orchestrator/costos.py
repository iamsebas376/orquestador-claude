"""Precios, costo estimado y registro de consumo de las delegaciones (y equivalente API de Claude)."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta

import registro as R
from registro import _now, _usd


_PRECIOS_CACHE: dict = {}  # (ruta, mtime) -> tabla; se vuelve a leer solo si el archivo cambia


def _precios() -> dict:
    try:
        clave = (str(R.PRECIOS_FILE), R.PRECIOS_FILE.stat().st_mtime_ns)
        if clave not in _PRECIOS_CACHE:
            _PRECIOS_CACHE.clear()
            _PRECIOS_CACHE[clave] = json.loads(R.PRECIOS_FILE.read_text(encoding="utf-8"))
        return _PRECIOS_CACHE[clave]
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
    R.LOGS_DIR.mkdir(parents=True, exist_ok=True)
    fila = {"ts": _now(), "agente": agente, "modelo": modelo, "tokens": tokens, "costo_usd": round(costo, 6),
            "pedido_id": pedido_id, "item": item, "ruta": ruta}
    if sesion:  # sesión de OpenCode o conversación de Antigravity: el panel cuenta sesiones distintas
        fila["sesion"] = sesion
    if equivalente:
        fila["costo_equivalente_usd"] = round(equivalente, 6)
    with open(R.COSTOS_FILE, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(fila, ensure_ascii=False) + "\n")


def resumen_costos(dias: int = 1) -> str:
    """Consumo de las delegaciones de los últimos `dias` días: total, por agente y por modelo."""
    desde = datetime.now() - timedelta(days=dias)
    filas = []
    if R.COSTOS_FILE.exists():
        for line in R.COSTOS_FILE.read_text(encoding="utf-8").splitlines():
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


def costos_diarios(dias: int = R.PANEL_DIAS, pedidos: set | None = None) -> list[dict]:
    """Costo agregado por día, agente y modelo (para filtrar por fechas en el panel).
    `pedidos`: si se indica, solo cuentan las filas de esos pedidos (los de sesiones eliminadas se omiten)."""
    desde = datetime.now() - timedelta(days=dias)
    agg: dict[tuple, dict] = {}
    if R.COSTOS_FILE.exists():
        for line in R.COSTOS_FILE.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
                ts = datetime.fromisoformat(r["ts"])
            except (ValueError, KeyError):
                continue
            if ts < desde or (pedidos is not None and r.get("pedido_id") not in pedidos):
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
    if R.COSTOS_FILE.exists():
        for line in R.COSTOS_FILE.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if not r.get("pedido_id"):
                continue
            t = r.get("tokens") or {}
            a = agg.setdefault((r["pedido_id"], r.get("item", 0)),
                               {"tokens_entrada": 0, "tokens_salida": 0, "costo_usd": 0.0, "costo_equivalente_usd": 0.0,
                                "agentes": {}, "ts": ""})
            a["ts"] = max(a["ts"], r.get("ts", ""))  # última vez que hubo trabajo real en el ítem
            a["tokens_entrada"] += t.get("input", 0) + t.get("cache_read", 0)
            a["tokens_salida"] += t.get("output", 0) + t.get("reasoning", 0)
            a["costo_usd"] += r.get("costo_usd", 0)
            a["costo_equivalente_usd"] += r.get("costo_equivalente_usd", 0)
            g = a["agentes"].setdefault(r.get("agente", "?"), {"tokens": 0, "costo_usd": 0.0, "costo_equivalente_usd": 0.0,
                                                               "modelos": []})
            if r.get("modelo") and r["modelo"] not in g["modelos"]:
                g["modelos"].append(r["modelo"])
            g["tokens"] += sum(t.get(k, 0) for k in ("input", "cache_read", "output", "reasoning"))
            g["costo_usd"] += r.get("costo_usd", 0)
            g["costo_equivalente_usd"] += r.get("costo_equivalente_usd", 0)
    return agg


def costo_claude(modelo: str, tokens: dict, fecha: str = "") -> float:
    """Equivalente por la API de Anthropic: escribir caché cuesta 1,25× la entrada (5 min y 1 h: así cuadra con
    el informe de uso de Claude Code)."""
    precio = _precio_vigente(modelo, fecha)
    if not precio:
        return 0.0
    escritura = (tokens.get("cache_write_5m", 0) + tokens.get("cache_write_1h", 0)) * 1.25
    return costo_innti(modelo, tokens, fecha) + escritura * precio[0] / 1_000_000
