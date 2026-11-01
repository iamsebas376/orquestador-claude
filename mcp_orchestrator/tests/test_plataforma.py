"""Tests de multiplataforma (Windows / macOS), comandos POSIX/PowerShell e instalador idempotente."""
import json
from pathlib import Path

import instalar
import plataforma


def test_exe_y_rutas_por_defecto(monkeypatch):
    monkeypatch.setattr(plataforma.sys, "platform", "win32")
    assert plataforma.es_windows() and not plataforma.es_macos()
    assert plataforma.exe("agy") == "agy.exe"
    assert plataforma.exe("opencode.cmd") == "opencode.cmd"
    assert plataforma.default_dir("workspaces").name == "workspaces"

    monkeypatch.setattr(plataforma.sys, "platform", "darwin")
    assert plataforma.es_macos() and not plataforma.es_windows()
    assert plataforma.exe("agy") == "agy"
    assert plataforma.exe("agy.exe") == "agy"


def test_config_claude_desktop_y_app_dir_por_so(tmp_path, monkeypatch):
    monkeypatch.setattr(plataforma.Path, "home", classmethod(lambda cls: tmp_path))

    # macOS
    monkeypatch.setattr(plataforma.sys, "platform", "darwin")
    assert plataforma.config_claude_desktop() == (
        tmp_path / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    )
    assert plataforma.config_claude_code() == tmp_path / ".claude.json"
    assert plataforma.claude_dir() == tmp_path / ".claude"
    assert plataforma.default_claude_app_dir() == tmp_path / "Library" / "Application Support" / "Claude"
    assert plataforma.venv_python(tmp_path / ".venv") == tmp_path / ".venv" / "bin" / "python"

    # Windows
    monkeypatch.setattr(plataforma.sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    assert plataforma.config_claude_desktop() == tmp_path / "Roaming" / "Claude" / "claude_desktop_config.json"
    assert plataforma.default_claude_app_dir() == tmp_path / "Local" / "Claude-Data"
    assert plataforma.venv_python(tmp_path / ".venv") == tmp_path / ".venv" / "Scripts" / "python.exe"


def test_generacion_comandos_powershell_y_posix(monkeypatch):
    # Windows -> PowerShell
    monkeypatch.setattr(plataforma.sys, "platform", "win32")
    cmd_win = plataforma.run_line(
        r"C:\Users\ana\.gemini\bin\agy.exe",
        "-i",
        plataforma.quote_prompt("Lee el archivo /tmp/a.md"),
        "--model",
        "gemini-3.8-flash-low",
        cwd=r"D:\personal\mi proy",
    )
    assert cmd_win.startswith("Set-Location 'D:\\personal\\mi proy'; & 'C:\\Users\\ana\\.gemini\\bin\\agy.exe'")
    assert '-i "Lee el archivo /tmp/a.md"' in cmd_win

    # macOS -> POSIX con quoting seguro (shlex.quote)
    monkeypatch.setattr(plataforma.sys, "platform", "darwin")
    cmd_mac = plataforma.run_line(
        "/Users/ana/.gemini/bin/agy",
        "-i",
        plataforma.quote_prompt("Lee el archivo /Users/ana/mi carpeta/p.md"),
        "--model",
        "gemini-3.8-flash-low",
        cwd="/Users/ana/personal/mi proy's",
    )
    assert cmd_mac.startswith("cd '/Users/ana/personal/mi proy'\"'\"'s' && /Users/ana/.gemini/bin/agy")
    assert "-i 'Lee el archivo /Users/ana/mi carpeta/p.md'" in cmd_mac
    assert "--model gemini-3.8-flash-low" in cmd_mac


def test_buscar_binarios_macos_y_windows(tmp_path, monkeypatch):
    monkeypatch.setattr(plataforma.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(plataforma.shutil, "which", lambda _name: None)
    monkeypatch.setattr(plataforma, "_npm_global_bin", lambda: [tmp_path / "npm-global" / "bin"])

    # Simulamos macOS con binarios en ~/.gemini/bin/agy y ~/.opencode/bin/opencode
    monkeypatch.setattr(plataforma.sys, "platform", "darwin")
    agy_mac = tmp_path / ".gemini" / "bin" / "agy"
    agy_mac.parent.mkdir(parents=True)
    agy_mac.write_text("#!/bin/sh\n", encoding="utf-8")

    oc_mac = tmp_path / ".opencode" / "bin" / "opencode"
    oc_mac.parent.mkdir(parents=True)
    oc_mac.write_text("#!/bin/sh\n", encoding="utf-8")

    assert plataforma.buscar_agy() == str(agy_mac)
    assert plataforma.buscar_opencode() == str(oc_mac)


def test_instalar_crea_env_con_contrasena(tmp_path, monkeypatch):
    (tmp_path / ".env.example").write_text("A=1\nOPENCODE_SERVER_PASSWORD=\n", encoding="utf-8")
    monkeypatch.setattr(instalar, "APP", tmp_path)
    monkeypatch.setattr(plataforma, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(plataforma, "DATOS_DIR", tmp_path / "datos")
    instalar.preparar_env()
    texto = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "A=1" in texto
    clave = texto.split("OPENCODE_SERVER_PASSWORD=")[1].strip()
    assert len(clave) >= 24
    instalar.preparar_env()  # idempotente: no cambia una contraseña existente
    assert clave in (tmp_path / ".env").read_text(encoding="utf-8")


def test_merge_idempotente_json_y_claude_md_en_macos(tmp_path, monkeypatch):
    for var in ("TRABAJO_DIR", "PERSONAL_DIR", "UNIVERSIDAD_DIR"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(plataforma.sys, "platform", "darwin")
    monkeypatch.setattr(plataforma.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(plataforma, "REPO_ROOT", tmp_path / "repo")
    monkeypatch.setattr(instalar, "APP", tmp_path / "repo" / "mcp_orchestrator")
    monkeypatch.setattr(instalar, "SERVER", tmp_path / "repo" / "mcp_orchestrator" / "server.py")

    cfg_dir = tmp_path / "repo" / "config"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "CLAUDE.example.md").write_text(
        "# Orquestador MCP: Claude es el cerebro\n- Trabajo: {{TRABAJO_DIR}}\n- Personal: D:\\personal\n",
        encoding="utf-8",
    )

    # Pre-poblar ~/.claude.json y ~/.claude/CLAUDE.md con contenido del usuario
    code_json = tmp_path / ".claude.json"
    code_json.write_text(json.dumps({"mcpServers": {"otro": {"command": "node", "args": ["x.js"]}}, "theme": "dark"}), encoding="utf-8")

    claude_md = tmp_path / ".claude" / "CLAUDE.md"
    claude_md.parent.mkdir(parents=True)
    claude_md.write_text("# Mis reglas personales\nNo borrar nunca esta nota.\n", encoding="utf-8")

    py_bin = tmp_path / "repo" / "mcp_orchestrator" / ".venv" / "bin" / "python"
    instalar.registrar(py_exec=py_bin)

    # 1. Verifica rutas de macOS
    desktop_json = tmp_path / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    assert desktop_json.is_file()
    d_data = json.loads(desktop_json.read_text(encoding="utf-8"))
    assert d_data["mcpServers"]["orchestrator"]["command"] == str(py_bin)

    # 2. Verifica merge en ~/.claude.json conservando "otro" y "theme"
    c_data = json.loads(code_json.read_text(encoding="utf-8"))
    assert c_data["theme"] == "dark"
    assert "otro" in c_data["mcpServers"]
    assert c_data["mcpServers"]["orchestrator"]["type"] == "stdio"
    assert c_data["mcpServers"]["orchestrator"]["command"] == str(py_bin)

    # 3. Verifica merge en ~/.claude/CLAUDE.md con marcadores, placeholders resueltos y sin pisar contenido del usuario
    md_text = claude_md.read_text(encoding="utf-8")
    assert "# Mis reglas personales" in md_text
    assert "No borrar nunca esta nota." in md_text
    assert instalar.MARCADOR_INICIO in md_text and instalar.MARCADOR_FIN in md_text
    assert str(tmp_path / "workspaces") in md_text
    assert str(tmp_path / "personal") in md_text
    assert "D:\\personal" not in md_text

    # 4. Segunda ejecución idempotente: no duplica marcadores ni hooks
    instalar.registrar(py_exec=py_bin)
    md_text_2 = claude_md.read_text(encoding="utf-8")
    assert md_text_2 == md_text
    assert md_text_2.count(instalar.MARCADOR_INICIO) == 1

    settings_data = json.loads((tmp_path / ".claude" / "settings.json").read_text(encoding="utf-8"))
    post_hooks = settings_data["hooks"]["PostToolUse"]
    assert sum(1 for h in post_hooks if h.get("matcher") == "mcp__orchestrator__cerrar_pedido") == 1


def test_instalar_flags_solo_diagnostico_y_dry_run(tmp_path, monkeypatch, capsys):
    for var in ("TRABAJO_DIR", "PERSONAL_DIR", "UNIVERSIDAD_DIR"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(plataforma.sys, "platform", "darwin")
    monkeypatch.setattr(plataforma.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(plataforma, "REPO_ROOT", tmp_path / "repo")
    monkeypatch.setattr(instalar, "APP", tmp_path / "repo" / "mcp_orchestrator")
    monkeypatch.setattr(instalar, "SERVER", tmp_path / "repo" / "mcp_orchestrator" / "server.py")

    # --solo-diagnostico no crea archivos
    assert instalar.main(["--solo-diagnostico"]) == 0
    out_diag = capsys.readouterr().out
    assert "Diagnóstico del Orquestador MCP" in out_diag
    assert not (tmp_path / ".claude.json").exists()

    # --dry-run simula sin escribir archivos
    assert instalar.main(["--dry-run"]) == 0
    out_dry = capsys.readouterr().out
    assert "[dry-run]" in out_dry
    assert not (tmp_path / ".claude.json").exists()

