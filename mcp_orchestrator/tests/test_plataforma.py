"""Sintaxis de comandos y terminales en Windows / PowerShell."""
import instalar
import plataforma


def test_safe_prompt_ascii_y_sin_caracteres_del_shell():
    out = plataforma.safe_prompt('Análisis de año ¿cómo? "hola" cuesta $5; ñandú `x` l\'agent')
    assert out.isascii() and "\n" not in out
    assert not set('"$;`') & set(out)
    assert "Analisis de ano como" in out and "nandu" in out


def test_quote_prompt():
    q = plataforma.quote_prompt("hola mundo")
    assert q == '"hola mundo"'


def test_run_line_windows_powershell():
    line = plataforma.run_line(r"C:\Program Files\agy.exe", "-i", '"x"', cwd=r"D:\personal\o'neil")
    assert line == "Set-Location 'D:\\personal\\o''neil'; & 'C:\\Program Files\\agy.exe' -i \"x\""


def test_run_line_sin_cwd():
    line = plataforma.run_line("/bin/py", "a")
    assert line.endswith("a") and "Set-Location" not in line


def test_exe_y_rutas_por_defecto():
    assert plataforma.exe("agy") == "agy.exe"
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
