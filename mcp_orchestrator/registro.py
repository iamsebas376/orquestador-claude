"""Registro del orquestador: rutas, constantes y ayudantes comunes, y fachada de los módulos pedidos, costos y
panel_datos (los consumidores siguen usando `registro.<nombre>`).

Todo se guarda como archivos en disco (JSON + Markdown legible) para que sobreviva entre sesiones de Claude.
Las rutas de abajo se consultan en tiempo de ejecución como `registro.X`, así los tests pueden cambiarlas."""

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

PRECIOS_FILE = Path(os.getenv("PRECIOS_FILE") or BASE_DIR / "precios.json")  # local; ver precios.example.json  # local; ver precios.example.json
CLAUDE_PROJECTS_DIR = Path(os.getenv("CLAUDE_PROJECTS_DIR") or Path.home() / ".claude" / "projects")
PANEL_DIAS = int(os.getenv("PANEL_DIAS") or "120")
PANEL_MAX_PEDIDOS = int(os.getenv("PANEL_MAX_PEDIDOS") or "200")

# Fachada: va al final porque los módulos importan las constantes de arriba.
from costos import (consumo_por_item, costo_claude, costo_innti, costos_diarios, registrar_costo,  # noqa: E402,F401
                    resumen_costos, _precio_vigente, _precios)
from panel_datos import consumo_claude, datos_panel  # noqa: E402,F401
from pedidos import (_load, _render, _save, agregar_items, actualizar_item, cerrar_pedido,  # noqa: E402,F401
                     consultar_aprendizajes, iniciar_pedido, item_automatico, listar_pedidos, proyecto_de,
                     registrar_aprendizaje, separar_ruta, ver_pedido, vincular_delegacion)

