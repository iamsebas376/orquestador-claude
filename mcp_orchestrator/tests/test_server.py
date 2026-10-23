import asyncio
import subprocess
from pathlib import Path

import registro
import server


def run(coro):
    return asyncio.run(coro)


# ----------------------------------------------------------------------------- rutas y enrutamiento

def test_resolve_personal_universidad_y_personal(tmp_dirs):
    assert server._resolve_personal(r"universidad\calculo\t.md") == tmp_dirs["universidad"] / "calculo" / "t.md"
    assert server._resolve_personal("bot") == tmp_dirs["personal"] / "bot"
    assert server._resolve_personal(str(tmp_dirs["universidad"] / "x")) == tmp_dirs["universidad"] / "x"


def test_inside_bloquea_rutas_fuera_y_traversal(tmp_dirs):
    assert server._inside(tmp_dirs["workspaces"] / "p", tmp_dirs["workspaces"])
    assert not server._inside(tmp_dirs["personal"], tmp_dirs["workspaces"])
    assert not server._inside(tmp_dirs["workspaces"] / ".." / "personal", tmp_dirs["workspaces"])


def test_trabajo_rechaza_ruta_personal(tmp_dirs):
    out = run(server.ejecutar_trabajo_opencode_innti("x", "", str(tmp_dirs["personal"])))
    assert out.startswith("Rechazado")


def test_personal_rechaza_ruta_de_trabajo(tmp_dirs):
    out = run(server.preparar_tarea_antigravity("x", "", str(tmp_dirs["workspaces"] / "p")))
    assert out.startswith("Rechazado")


def test_work_cwd_crea_carpeta_padre_para_archivo(tmp_dirs):
    target = tmp_dirs["workspaces"] / "nuevo" / "a.py"
    assert server._work_cwd(target) == target.parent and target.parent.is_dir()


# ----------------------------------------------------------------------------- comandos para la terminal

def test_preparar_tarea_antigravity_windows(tmp_dirs, monkeypatch):
    monkeypatch.setattr(server.plataforma, "IS_WINDOWS", True)
    monkeypatch.setattr(server, "_agy_cmd", lambda: r"C:\fake\agy.exe")
    out = run(server.preparar_tarea_antigravity("Crea notas", "", "proyecto", "gemini-3.8-flash-low"))
    cmd = out.split("\n")[-1]
    assert cmd.startswith(f"Set-Location '{tmp_dirs['personal'] / 'proyecto'}'; & 'C:\\fake\\agy.exe' -i \"")
    assert cmd.endswith("--model gemini-3.8-flash-low --mode accept-edits")
    assert cmd.isascii()
    task_id = out.split("\n")[0].split(": ")[1]
    assert task_id in server._AGY_TASKS


def test_prompt_de_antigravity_pide_ortografia(tmp_dirs, monkeypatch):
    monkeypatch.setattr(server, "_agy_cmd", lambda: "agy")
    out = run(server.preparar_tarea_antigravity("Crea un README", "", "p", "m"))
    cmd = out.split("\n")[-1]
    assert "ORTOGRAFIA" in cmd and "tildes" in cmd and "virgulilla" in cmd and cmd.isascii()


def test_preparar_vista_opencode_no_expone_contrasena(tmp_dirs, monkeypatch):
    async def ok(_cli):
        return True

    async def sesion(_cwd, _t):
        return "ses_prueba"

    monkeypatch.setattr(server, "_opencode_cmd", lambda: r"C:\fake\opencode.exe")
    monkeypatch.setattr(server, "_ensure_opencode_server", ok)
    monkeypatch.setattr(server, "_create_session", sesion)
    monkeypatch.setenv("OPENCODE_SERVER_PASSWORD", "secreto-123")
    out = run(server.preparar_vista_opencode("proyecto"))
    assert "session_id: ses_prueba" in out
    assert "oc_attach.py" in out and "--session ses_prueba" in out
    assert "secreto-123" not in out


def test_oc_auth_lee_la_contrasena(monkeypatch):
    monkeypatch.setenv("OPENCODE_SERVER_PASSWORD", "abc")
    monkeypatch.delenv("OPENCODE_SERVER_USERNAME", raising=False)
    assert server._oc_auth() == ("opencode", "abc")
    monkeypatch.delenv("OPENCODE_SERVER_PASSWORD")
    assert server._oc_auth() is None


# ----------------------------------------------------------------------------- git y panel

def test_git_checkpoint(tmp_path):
    assert server._git_checkpoint(tmp_path) == ""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "a.txt").write_text("x")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "i"],
                   check=True)
    (tmp_path / "b.txt").write_text("y")
    out = server._git_checkpoint(tmp_path)
    assert out.startswith("commit ") and "+1 cambios previos" in out


def test_exportar_panel_crea_y_borra_el_anterior(tmp_registro, monkeypatch):
    monkeypatch.setattr(server, "PANEL_URL", "https://claude.ai/artifact/prueba")
    registro.iniciar_pedido("P", ["a"])
    primero = server.exportar_panel()
    assert '"op": "set"' in primero and '"op": "delete"' not in primero
    ultimo = (registro.TAREAS_DIR / "panel" / "ultimo.txt").read_text()
    (registro.TAREAS_DIR / "panel" / "ultimo.txt").write_text("s-anterior")  # simula una sincronización previa
    segundo = server.exportar_panel()
    assert '"doc_id": "s-anterior", "if_version": 1' in segundo
    assert ultimo.startswith("s-")
    assert Path(registro.TAREAS_DIR / "panel").is_dir()


def test_todas_las_herramientas_registradas():
    nombres = {t.name for t in run(server.mcp.list_tools())}
    esperadas = {"listar_modelos_innti", "preparar_vista_opencode", "ejecutar_trabajo_opencode_innti",
                 "ejecutar_personal_antigravity", "preparar_tarea_antigravity", "esperar_resultado_antigravity",
                 "iniciar_pedido", "actualizar_item", "agregar_items", "cerrar_pedido", "ver_pedido",
                 "listar_pedidos", "resumen_costos", "registrar_aprendizaje", "consultar_aprendizajes",
                 "exportar_panel", "ver_configuracion", "verificar_proyecto", "configurar_verificacion",
                 "estado_presupuesto"}
    assert esperadas <= nombres


# ----------------------------------------------------------------------------- límite de modelos de Antigravity

def _transcript(tmp_path, monkeypatch, pasos):
    import json
    monkeypatch.setattr(server.Path, "home", lambda: tmp_path)
    conv = "0123abcd-0000-0000-0000-000000000000"
    d = tmp_path / ".gemini" / "antigravity-cli" / "brain" / conv / ".system_generated" / "logs"
    d.mkdir(parents=True)
    (d / "transcript_full.jsonl").write_text("\n".join(json.dumps(p) for p in pasos), encoding="utf-8")
    return conv


def test_siguiente_modelo_sigue_la_cadena(monkeypatch):
    monkeypatch.setattr(server, "ANTIGRAVITY_CADENA", ["a-high", "a-medium", "f-medium", "f-low"])
    assert server._siguiente_modelo("a-high") == "a-medium"
    assert server._siguiente_modelo("a-medium", ["a-high", "a-medium"]) == "f-medium"
    assert server._siguiente_modelo("f-medium", ["f-low"]) == ""
    assert server._siguiente_modelo("f-low") == ""
    assert server._siguiente_modelo("otro") == "a-high"


def test_detecta_limite_y_red_en_el_transcript(tmp_path, monkeypatch):
    from datetime import datetime, timezone
    inicio = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)
    t = "2026-10-18T12:01:00Z"
    conv = _transcript(tmp_path, monkeypatch, [
        {"type": "ERROR_MESSAGE", "created_at": "2026-10-18T11:00:00Z", "error": "API error: 429 quota"},  # antes
        {"type": "PLANNER_RESPONSE", "created_at": t, "input_tokens": 10, "output_tokens": 2},
    ])
    assert server._agy_fallo(conv, inicio) == ("", "")  # el error es de una ronda anterior

    conv = _transcript(tmp_path / "b", monkeypatch, [
        {"type": "ERROR_MESSAGE", "created_at": t, "error": "API error (attempt 1): 429 RESOURCE_EXHAUSTED"},
    ])
    assert server._agy_fallo(conv, inicio)[0] == "limite"

    red = [{"type": "ERROR_MESSAGE", "created_at": t, "error": "dial tcp: lookup x: no such host"}] * 6
    conv = _transcript(tmp_path / "c", monkeypatch, red)
    assert server._agy_fallo(conv, inicio)[0] == "red"
    conv = _transcript(tmp_path / "d", monkeypatch, red + [{"type": "PLANNER_RESPONSE", "created_at": t}])
    assert server._agy_fallo(conv, inicio) == ("", "")  # agy reintentó y siguió


def test_espera_corta_al_llegar_al_limite(tmp_dirs, tmp_registro, monkeypatch):
    from conftest import pedido_id
    dormir = asyncio.sleep
    monkeypatch.setattr(server, "ANTIGRAVITY_CADENA", ["a-high", "a-medium"])
    monkeypatch.setattr(server, "_agy_conversacion", lambda folder: "")
    monkeypatch.setattr(server, "_agy_fallo", lambda conv, desde: ("limite", "429 RESOURCE_EXHAUSTED"))
    monkeypatch.setattr(server.asyncio, "sleep", lambda s: dormir(0))
    folder = tmp_dirs["personal"] / "p"
    folder.mkdir()
    pid = pedido_id(registro.iniciar_pedido("Límite", ["A"]))
    task = server._agy_prepare("tarea", "", folder, folder, "a-high")
    task.update(pedido_id=pid, item=1)
    res = run(server.esperar_resultado_antigravity(task["id"]))
    assert res.startswith("Límite") and "siguiente_modelo: a-medium" in res
    assert "modelos_descartados: a-high" in res and "preparar_tarea_antigravity" in res
    assert task["id"] not in server._AGY_TASKS
    assert "Límite de a-high" in registro.ver_pedido(pid)


def test_confia_solo_en_carpetas_personales(tmp_dirs, tmp_path, monkeypatch):
    import json
    f = tmp_path / "agy-settings.json"
    f.write_text(json.dumps({"permissions": {"allow": ["command(npm test)"]}, "trustedWorkspaces": []}), encoding="utf-8")
    monkeypatch.setattr(server, "AGY_SETTINGS", f)
    proyecto = tmp_dirs["personal"] / "app"
    assert server._agy_confiar(proyecto) and server._agy_confiar(proyecto)  # la segunda no la repite
    assert not server._agy_confiar(tmp_dirs["workspaces"] / "trabajo")
    data = json.loads(f.read_text(encoding="utf-8"))
    assert data["trustedWorkspaces"] == [str(proyecto)]
    assert data["permissions"]["allow"] == ["command(npm test)"]  # el resto de la configuración se conserva


def test_timeout_reporta_tokens_y_actualiza_inicio(tmp_dirs, monkeypatch):
    """En caso de tiempo de espera agotado (timeout), _agy_wait reporta tokens y conversación, y actualiza task['inicio']."""
    from datetime import datetime, timezone
    dormir = asyncio.sleep
    monkeypatch.setattr(server, "ANTIGRAVITY_TIMEOUT", 0)
    monkeypatch.setattr(server.asyncio, "sleep", lambda s: dormir(0))
    monkeypatch.setattr(server, "_agy_fallo", lambda conv, desde: ("", ""))
    monkeypatch.setattr(server, "_agy_conversacion", lambda folder: "0123abcd-0000-0000-0000-000000000000")

    llamadas_tokens = []

    def fake_tokens(conv, desde):
        llamadas_tokens.append((conv, desde))
        return {"input": 1200, "cache_read": 300, "output": 150}

    monkeypatch.setattr(server, "_agy_tokens", fake_tokens)

    folder = tmp_dirs["personal"] / "p"
    folder.mkdir(exist_ok=True)
    inicio_fijado = datetime(2026, 10, 21, 10, 0, 0, tzinfo=timezone.utc)
    task = server._agy_prepare("tarea_timeout", "", folder, folder, "gemini-3.8-flash-medium")
    task["inicio"] = inicio_fijado
    task_id = task["id"]

    res = run(server._agy_wait(task, "terminal de Claude"))

    assert res.startswith("Sin resultado:")
    assert "Tokens: 1.500 entrada (300 de caché) / 150 salida" in res
    assert "conversacion_antigravity: 0123abcd-0000-0000-0000-000000000000" in res
    assert f"task_id: {task_id}" in res
    assert task_id in server._AGY_TASKS
    # Verifica que task['inicio'] se haya actualizado a un instante posterior para no duplicar el cómputo de tokens.
    assert task["inicio"] > inicio_fijado
    assert len(llamadas_tokens) == 1
    assert llamadas_tokens[0] == ("0123abcd-0000-0000-0000-000000000000", inicio_fijado)
