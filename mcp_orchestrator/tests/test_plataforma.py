"""Rutas y nombres por defecto (Windows) e instalador."""
import instalar
import plataforma


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
