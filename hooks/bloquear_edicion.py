"""Hook PreToolUse de Claude Code: bloquea que Claude edite o cree archivos en las carpetas de proyectos.

En esas carpetas Claude solo delega y revisa; los cambios los hacen OpenCode (trabajo) o Antigravity (personal).
Se permite el cambio trivial de la regla: una edición puntual de hasta 3 líneas (Edit sin replace_all).
"""
import json
import os
import sys

CARPETAS = [r"D:\workspaces", r"D:\personal", r"D:\universidad"]
MAX_LINEAS = 3


def dentro(ruta: str) -> bool:
    r = os.path.normcase(os.path.abspath(ruta))
    return any(r == os.path.normcase(c) or r.startswith(os.path.normcase(c) + os.sep) for c in CARPETAS)


def lineas(texto: str) -> int:
    return len((texto or "").splitlines()) or 1


def main() -> None:
    try:
        evento = json.load(sys.stdin)
    except ValueError:
        return  # sin datos legibles no se bloquea nada
    herramienta = evento.get("tool_name", "")
    entrada = evento.get("tool_input") or {}
    ruta = entrada.get("file_path") or entrada.get("notebook_path") or ""
    if not ruta or not dentro(ruta):
        return
    if herramienta == "Edit" and not entrada.get("replace_all") and \
            max(lineas(entrada.get("old_string")), lineas(entrada.get("new_string"))) <= MAX_LINEAS:
        return  # cambio trivial permitido (debe anotarse en el ítem: "Claude corrigió: …")
    trabajo = os.path.normcase(os.path.abspath(ruta)).startswith(os.path.normcase(CARPETAS[0]))
    agente = "OpenCode (ejecutar_trabajo_opencode_innti)" if trabajo else "Antigravity (preparar_tarea_antigravity)"
    motivo = (f"Bloqueado por tu regla: en {ruta} no editas ni creas archivos, solo delegas y revisas. "
              f"Delega este cambio a {agente} con pedido_id e ítem; solo se permite un cambio trivial de hasta "
              f"{MAX_LINEAS} líneas con Edit, anotado en el ítem.")
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                             "permissionDecisionReason": motivo}}, ensure_ascii=False))


if __name__ == "__main__":
    main()
