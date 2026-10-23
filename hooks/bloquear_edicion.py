"""Hook PreToolUse de Claude Code: Claude no edita ni crea archivos; solo delega y revisa.

Aplica en cualquier carpeta. Los cambios los hacen OpenCode con Innti (solo D:\\workspaces) o Antigravity (todo lo
demás). Excepciones: la carpeta propia de Claude (~/.claude: memoria, planes), la carpeta temporal (scratchpad) y el
cambio trivial de la regla: una edición puntual de hasta 3 líneas (Edit sin replace_all).
"""
import json
import os
import sys
import tempfile

TRABAJO = r"D:\workspaces"
MAX_LINEAS = 3
PERMITIDAS = [os.path.join(os.path.expanduser("~"), ".claude"), tempfile.gettempdir()]


def dentro(ruta: str, carpeta: str) -> bool:
    r, c = os.path.normcase(os.path.abspath(ruta)), os.path.normcase(os.path.abspath(carpeta))
    return r == c or r.startswith(c + os.sep)


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
    if not ruta or any(dentro(ruta, c) for c in PERMITIDAS):
        return
    if herramienta == "Edit" and not entrada.get("replace_all") and \
            max(lineas(entrada.get("old_string")), lineas(entrada.get("new_string"))) <= MAX_LINEAS:
        return  # cambio trivial permitido (debe anotarse en el ítem: "Claude corrigió: …")
    agente = ("OpenCode con Innti (ejecutar_trabajo_opencode_innti)" if dentro(ruta, TRABAJO)
              else "Antigravity (ejecutar_personal_antigravity)")
    motivo = (f"Bloqueado por tu regla: no editas ni creas archivos ({ruta}); solo delegas y revisas. "
              f"Delega este cambio a {agente} con pedido_id e ítem. Solo se permite un cambio trivial de hasta "
              f"{MAX_LINEAS} líneas con Edit, anotado en el ítem.")
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                             "permissionDecisionReason": motivo}}, ensure_ascii=False))


if __name__ == "__main__":
    main()
