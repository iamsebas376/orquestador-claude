"""Ejecuta una herramienta del orquestador sin cliente MCP (mismo código que usa Claude).

Uso: python probar.py <herramienta> '<json con argumentos>'
Ej.: python probar.py listar_modelos_innti '{}'
"""
import asyncio
import json
import sys

import server

if __name__ == "__main__":
    raw = sys.argv[2] if len(sys.argv) > 2 else "{}"
    if raw.startswith("@"):  # @ruta.json: argumentos desde archivo
        raw = open(raw[1:], encoding="utf-8").read()
    nombre, args = sys.argv[1], json.loads(raw)
    resultado = getattr(server, nombre)(**args)
    print(asyncio.run(resultado) if asyncio.iscoroutine(resultado) else resultado)
