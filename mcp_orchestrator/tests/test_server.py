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
    (tmp_path / "a.txt").write_text("x\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "i"],
                   check=True)
    (tmp_path / "b.txt").write_text("y\nz\n")
    out = server._git_checkpoint(tmp_path)
    assert out.startswith("commit ") and "+1 cambios previos" in out
    changed, lineas = server._git_cambios(tmp_path)
    assert changed == [str((tmp_path / "b.txt").resolve())] and lineas == 2


def test_trabajo_exige_sesion_opencode_salvo_sin_terminal(tmp_dirs, monkeypatch):
    monkeypatch.setattr(server, "_opencode_cmd", lambda: "opencode")
    err = run(server.ejecutar_trabajo_opencode_innti("x", "", str(tmp_dirs["workspaces"] / "p")))
    assert err.startswith("Error: falta `sesion_opencode`") and "preparar_vista_opencode" in err


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
    esperadas = {"preparar_vista_opencode", "ejecutar_trabajo_opencode_innti", "preparar_tarea_antigravity",
                 "esperar_resultado_antigravity",
                 "iniciar_pedido", "actualizar_item", "agregar_items", "cerrar_pedido", "ver_pedido",
                 "listar_pedidos", "resumen_costos", "registrar_aprendizaje", "consultar_aprendizajes",
                 "exportar_panel", "verificar_proyecto", "configurar_verificacion"}
    assert nombres == esperadas


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


def _preparar(tmp_dirs, monkeypatch, modelo="gemini-3.8-flash-low", **kw):
    monkeypatch.setattr(server, "_agy_cmd", lambda: "agy")
    monkeypatch.setattr(server, "_agy_conversacion", lambda folder: "")
    monkeypatch.setattr(server, "_agy_fallo", lambda conv, desde: ("", ""))
    dormir = asyncio.sleep
    monkeypatch.setattr(server.asyncio, "sleep", lambda s: dormir(0))
    out = run(server.preparar_tarea_antigravity("Crea una nota con ñ y acentos", "", "p", modelo, **kw))
    task_id = out.split("\n")[0].split(": ")[1]
    return out, server._AGY_TASKS[task_id]


def test_preparar_antigravity_da_un_comando_corto_ascii_y_el_prompt_va_en_un_archivo(tmp_dirs, monkeypatch):
    monkeypatch.setattr(server, "ANTIGRAVITY_SIN_PERMISOS", True)
    out, task = _preparar(tmp_dirs, monkeypatch)
    cmd = out.split("\n")[-1]
    assert cmd.startswith(f"Set-Location '{tmp_dirs['personal'] / 'p'}'; & 'agy' -i \"Lee el archivo ")
    assert cmd.isascii() and len(cmd.encode()) < 2000
    assert "--model gemini-3.8-flash-low --mode accept-edits --dangerously-skip-permissions" in cmd
    texto = task["prompt_file"].read_text(encoding="utf-8")
    assert "ñ" in texto and str(task["resultado"]) in texto and "## RESUMEN" in texto


def test_preparar_antigravity_sin_saltar_permisos_si_se_desactiva(tmp_dirs, monkeypatch):
    monkeypatch.setattr(server, "ANTIGRAVITY_SIN_PERMISOS", False)
    out, _ = _preparar(tmp_dirs, monkeypatch)
    assert "--dangerously-skip-permissions" not in out


def test_esperar_resultado_devuelve_la_sintesis_y_vincula_el_item(tmp_dirs, tmp_registro, monkeypatch):
    from conftest import pedido_id
    pid = pedido_id(registro.iniciar_pedido("Con Antigravity", ["A"]))
    _, task = _preparar(tmp_dirs, monkeypatch, pedido_id=pid, item=1)
    task["resultado"].write_text("## RESUMEN\n- Creó la nota con acentos.", encoding="utf-8")
    res = run(server.esperar_resultado_antigravity(task["id"]))
    assert res.startswith("Éxito") and "Creó la nota" in res and "pestaña de Antigravity sigue abierta" in res
    assert "🔍" in registro.ver_pedido(pid)  # el ítem quedó en revisión
    assert task["id"] not in server._AGY_TASKS


def test_esperar_corta_al_llegar_al_limite_y_propone_el_siguiente_modelo(tmp_dirs, tmp_registro, monkeypatch):
    from conftest import pedido_id
    pid = pedido_id(registro.iniciar_pedido("Límite", ["A"]))
    monkeypatch.setattr(server, "ANTIGRAVITY_CADENA", ["a-high", "a-medium"])
    _, task = _preparar(tmp_dirs, monkeypatch, modelo="a-high", pedido_id=pid, item=1)
    monkeypatch.setattr(server, "_agy_fallo", lambda conv, desde: ("limite", "429 RESOURCE_EXHAUSTED"))
    res = run(server.esperar_resultado_antigravity(task["id"]))
    assert res.startswith("Límite") and "siguiente_modelo: a-medium" in res and "modelos_descartados: a-high" in res
    assert "preparar_tarea_antigravity" in res and "Límite de a-high" in registro.ver_pedido(pid)


def test_esperar_sin_resultado_a_tiempo_deja_la_tarea_pendiente(tmp_dirs, monkeypatch):
    monkeypatch.setattr(server, "ANTIGRAVITY_TIMEOUT", 0)
    _, task = _preparar(tmp_dirs, monkeypatch)
    res = run(server.esperar_resultado_antigravity(task["id"]))
    assert res.startswith("Sin resultado") and task["id"] in server._AGY_TASKS


def test_preparar_vista_opencode_no_expone_la_contrasena(tmp_dirs, monkeypatch):
    async def ok(_cli):
        return True

    async def sesion(_cwd, _t):
        return "ses_prueba"

    monkeypatch.setattr(server, "_opencode_cmd", lambda: "opencode")
    monkeypatch.setattr(server, "_ensure_opencode_server", ok)
    monkeypatch.setattr(server, "_create_session", sesion)
    monkeypatch.setenv("OPENCODE_SERVER_PASSWORD", "secreto-123")
    out = run(server.preparar_vista_opencode("proyecto"))
    assert "session_id: ses_prueba" in out and "oc_attach.py" in out and "--session ses_prueba" in out
    assert "secreto-123" not in out


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


def test_run_corta_por_inactividad(tmp_path):
    import sys
    code, out = run(server._run([sys.executable, "-c", "import time; print('a', flush=True); time.sleep(30)"],
                                tmp_path, 60, idle=1))
    assert code == -1 and "Sin salida durante 1s" in out and out.startswith("a")
