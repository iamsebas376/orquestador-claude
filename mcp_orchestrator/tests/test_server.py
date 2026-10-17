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


def test_preparar_tarea_antigravity_mac(tmp_dirs, monkeypatch):
    monkeypatch.setattr(server.plataforma, "IS_WINDOWS", False)
    monkeypatch.setattr(server, "_agy_cmd", lambda: "/Users/ana/.gemini/bin/agy")
    out = run(server.preparar_tarea_antigravity("Crea notas l'agent", "", "proyecto", "gemini-3.8-flash-low"))
    cmd = out.split("\n")[-1]
    assert cmd.startswith("cd ") and " && /Users/ana/.gemini/bin/agy -i '" in cmd
    assert "Set-Location" not in cmd and cmd.isascii()
    prompt = cmd.split(" -i '")[1].split("' --model")[0]
    assert "'" not in prompt


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
                 "exportar_panel", "ver_configuracion"}
    assert esperadas <= nombres
