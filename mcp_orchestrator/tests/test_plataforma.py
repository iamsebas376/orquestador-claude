"""Sintaxis de comandos y terminales por sistema operativo (se simulan Windows y macOS en cualquier equipo)."""
import shlex

import pytest

import instalar
import plataforma


@pytest.fixture(params=["windows", "mac"])
def sistema(request, monkeypatch):
    monkeypatch.setattr(plataforma, "IS_WINDOWS", request.param == "windows")
    monkeypatch.setattr(plataforma, "IS_MAC", request.param == "mac")
    return request.param


def test_safe_prompt_ascii_y_sin_caracteres_del_shell(sistema):
    out = plataforma.safe_prompt('Análisis de año ¿cómo? "hola" cuesta $5; ñandú `x` l\'agent')
    assert out.isascii() and "\n" not in out
    assert not set('"$;`') & set(out)
    assert "Analisis de ano como" in out and "nandu" in out
    if sistema == "mac":
        assert "'" not in out  # va entre comillas simples en zsh


def test_quote_prompt_segun_shell(sistema):
    q = plataforma.quote_prompt("hola mundo")
    assert q == ('"hola mundo"' if sistema == "windows" else "'hola mundo'")


def test_run_line_windows_powershell(monkeypatch):
    monkeypatch.setattr(plataforma, "IS_WINDOWS", True)
    line = plataforma.run_line(r"C:\Program Files\agy.exe", "-i", '"x"', cwd=r"D:\personal\o'neil")
    assert line == "Set-Location 'D:\\personal\\o''neil'; & 'C:\\Program Files\\agy.exe' -i \"x\""


def test_run_line_mac_zsh(monkeypatch):
    monkeypatch.setattr(plataforma, "IS_WINDOWS", False)
    line = plataforma.run_line("/Users/ana/.gemini/bin/agy", "-i", "'x'", cwd="/Users/ana/mis proyectos")
    assert line == "cd '/Users/ana/mis proyectos' && /Users/ana/.gemini/bin/agy -i 'x'"
    assert shlex.split(line.split(" && ")[1]) == ["/Users/ana/.gemini/bin/agy", "-i", "x"]


def test_run_line_sin_cwd(sistema):
    line = plataforma.run_line("/bin/py", "a")
    assert line.endswith("a") and "Set-Location" not in line and "cd " not in line


def test_open_terminal_mac_usa_terminal_app(monkeypatch, tmp_path):
    monkeypatch.setattr(plataforma, "IS_WINDOWS", False)
    monkeypatch.setattr(plataforma, "IS_MAC", True)
    llamadas = []
    monkeypatch.setattr(plataforma.subprocess, "Popen", lambda args, **kw: llamadas.append(args))
    err = plataforma.open_terminal("t", ["/bin/agy", "-i", 'di "hola"'], tmp_path,
                                   {"OPENCODE_SERVER_PASSWORD": "s3", "PATH": "/x"})
    assert err == ""
    osa = llamadas[0]
    assert osa[0] == "osascript" and 'tell application "Terminal"' in osa[2]
    assert "OPENCODE_SERVER_PASSWORD=s3" in osa[2] and "PATH=" not in osa[2]
    assert '\\"hola\\"' in osa[2]  # comillas escapadas para AppleScript


def test_exe_y_rutas_por_defecto(sistema):
    assert plataforma.exe("agy") == ("agy.exe" if sistema == "windows" else "agy")
    assert plataforma.default_dir("workspaces").name == "workspaces"


def test_instalar_crea_env_con_contrasena(tmp_path, monkeypatch):
    (tmp_path / ".env.example").write_text("A=1\nOPENCODE_SERVER_PASSWORD=\n", encoding="utf-8")
    monkeypatch.setattr(instalar, "APP", tmp_path)
    monkeypatch.setattr(plataforma, "REPO_ROOT", tmp_path)
    instalar.preparar_env()
    texto = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "A=1" in texto
    clave = texto.split("OPENCODE_SERVER_PASSWORD=")[1].strip()
    assert len(clave) >= 24
    instalar.preparar_env()  # idempotente: no cambia una contraseña existente
    assert clave in (tmp_path / ".env").read_text(encoding="utf-8")


def test_config_claude_desktop_por_sistema(sistema):
    p = instalar.config_claude_desktop() if sistema == "mac" else None
    if sistema == "mac":
        assert "Library/Application Support/Claude" in p.as_posix()
