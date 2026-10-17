"""Grupo 2: verificación por proyecto, trabajo aislado en worktrees, presupuesto y conversación de Antigravity."""
import asyncio
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

import control
import registro
import server


def run(coro):
    return asyncio.run(coro)


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), "-c", "user.email=t@t", "-c", "user.name=t", *args],
                          check=True, capture_output=True, text=True).stdout


@pytest.fixture
def tmp_control(tmp_path, monkeypatch):
    monkeypatch.setattr(control, "VERIF_FILE", tmp_path / "verificacion.json")
    monkeypatch.setattr(control, "WORKTREES_FILE", tmp_path / "worktrees.json")
    monkeypatch.setattr(registro, "COSTOS_FILE", tmp_path / "costos.jsonl")
    for v in ("PRESUPUESTO_DIA_USD", "PRESUPUESTO_MES_USD", "PRESUPUESTO_AVISO_PCT"):
        monkeypatch.delenv(v, raising=False)
    return tmp_path


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "ws" / "app"
    r.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(r)], check=True)
    (r / "a.py").write_text("x = 1\n")
    git(r, "add", ".")
    git(r, "commit", "-qm", "inicial")
    return r


# ----------------------------------------------------------------------------- verificación

def test_detecta_maven_npm_y_pytest(tmp_path):
    (tmp_path / "m").mkdir()
    (tmp_path / "m" / "pom.xml").write_text("<project/>")
    assert control.detectar_comandos(tmp_path / "m", tmp_path)[1] == ["mvn -q -B test"]
    (tmp_path / "n").mkdir()
    (tmp_path / "n" / "package.json").write_text(json.dumps({"scripts": {"lint": "x", "test": "jest"}}))
    assert control.detectar_comandos(tmp_path / "n", tmp_path)[1] == ["npm run lint --silent", "npm test --silent"]
    (tmp_path / "p").mkdir()
    (tmp_path / "p" / "test_x.py").write_text("")
    assert control.detectar_comandos(tmp_path / "p", tmp_path)[1] == ["python -m pytest -q"]


def test_npm_sin_test_real_no_se_ejecuta(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"test": "echo \"Error: no test specified\""}}))
    assert control.detectar_comandos(tmp_path, tmp_path)[1] == []


def test_busca_hacia_arriba_hasta_el_tope(tmp_path):
    proj = tmp_path / "proj"
    (proj / "src" / "main").mkdir(parents=True)
    (proj / "pom.xml").write_text("<project/>")
    carpeta, cmds = control.detectar_comandos(proj / "src" / "main", tmp_path)
    assert carpeta == proj and cmds
    carpeta, cmds = control.detectar_comandos(tmp_path / "otro", tmp_path)
    assert cmds == []


def test_configuracion_por_proyecto_tiene_prioridad(tmp_control, tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    (ws / "api").mkdir(parents=True)
    (ws / "api" / "pom.xml").write_text("<project/>")
    monkeypatch.setenv("TRABAJO_DIR", str(ws))
    control.guardar_verificacion("api", ["mvn -q -B verify"])
    assert control.comandos_para(ws / "api", ws)[1:] == (["mvn -q -B verify"], "configurados")


def test_verificar_verde_y_rojo(tmp_control, tmp_path):
    ok = f'"{sys.executable}" -c "print(1)"'
    malo = f'"{sys.executable}" -c "import sys; print(\'fallo X\'); sys.exit(3)"'
    verde = run(control.verificar(tmp_path, tmp_path, [ok], tmp_path / "logs"))
    assert verde.startswith("Verificación VERDE") and "✅" in verde
    rojo = run(control.verificar(tmp_path, tmp_path, [ok, malo], tmp_path / "logs"))
    assert rojo.startswith("Verificación ROJO") and "exit 3" in rojo and "fallo X" in rojo
    assert len(list((tmp_path / "logs").glob("*.log"))) == 2


def test_verificar_avisa_sin_node_modules(tmp_control, tmp_path):
    (tmp_path / "package.json").write_text("{}")
    out = run(control.verificar(tmp_path, tmp_path, [f'"{sys.executable}" -c "0"'], tmp_path / "logs"))
    assert "node_modules" in out


# ----------------------------------------------------------------------------- worktrees

def test_worktree_aplicar_deja_cambios_en_staging(tmp_control, repo):
    info = control.crear_worktree(repo / "a.py", "t1", repo.parent)
    wt = info["worktree"]
    assert info["rama"] == "orq/t1" and info["target"] == (wt / "a.py").resolve()
    assert control.worktree_info(wt / "a.py")["repo"] == str(repo.resolve())
    (wt / "a.py").write_text("x = 2\n")
    (wt / "b.py").write_text("y = 1\n")
    assert control.confirmar_cambios(wt, "orq: cambio")
    assert (repo / "a.py").read_text() == "x = 1\n"  # el repo original no se tocó
    out = control.integrar(str(wt), "aplicar")
    assert "staging" in out
    assert (repo / "a.py").read_text() == "x = 2\n" and (repo / "b.py").exists()
    assert "A  b.py" in git(repo, "status", "--porcelain")  # en staging, sin commit
    assert not wt.exists() and "orq/t1" not in git(repo, "branch")
    assert control.listar_worktrees().startswith("No hay")


def test_worktree_descartar_no_toca_el_repo(tmp_control, repo):
    wt = control.crear_worktree(repo, "t2", repo.parent)["worktree"]
    (wt / "a.py").write_text("roto\n")
    control.confirmar_cambios(wt, "orq: x")
    assert "Descartado" in control.integrar(str(wt), "descartar")
    assert (repo / "a.py").read_text() == "x = 1\n"
    assert git(repo, "status", "--porcelain") == ""


def test_verificacion_configurada_vale_dentro_del_worktree(tmp_control, repo, monkeypatch):
    monkeypatch.setenv("TRABAJO_DIR", str(repo.parent))
    control.guardar_verificacion("app", ["python -m unittest -q"])
    wt = control.crear_worktree(repo, "t4", repo.parent)["worktree"]
    carpeta, cmds, origen = control.comandos_para(wt, repo.parent)
    assert cmds == ["python -m unittest -q"] and origen == "configurados" and carpeta == wt
    control.integrar(str(wt), "descartar")


def test_worktree_no_aplica_en_carpeta_sin_git(tmp_control, tmp_path):
    (tmp_path / "plano").mkdir()
    assert control.crear_worktree(tmp_path / "plano", "t3", tmp_path) is None


def test_aislar_reutiliza_el_worktree_en_correcciones(tmp_control, repo, monkeypatch):
    monkeypatch.setattr(server, "TRABAJO_DIR", repo.parent)
    target, wt, aviso = server._aislar(repo, None, "tarea uno")
    assert wt and Path(wt["worktree"]).exists() and aviso == ""
    target2, wt2, _ = server._aislar(target, None, "corrección")
    assert target2 == target and wt2["worktree"] == wt["worktree"]
    control.integrar(wt["worktree"], "descartar")


# ----------------------------------------------------------------------------- presupuesto

def _gastar(usd):
    registro.registrar_costo("opencode", "m", {}, usd)


def test_presupuesto_sin_limite(tmp_control):
    _gastar(5)
    bloqueado, msg = control.estado_presupuesto()
    assert not bloqueado and msg.startswith("✅") and "sin límite" in msg


def test_presupuesto_avisa_y_frena(tmp_control, monkeypatch):
    monkeypatch.setenv("PRESUPUESTO_DIA_USD", "1")
    _gastar(0.85)
    bloqueado, msg = control.estado_presupuesto()
    assert not bloqueado and msg.startswith("⚠")
    _gastar(0.2)
    bloqueado, msg = control.estado_presupuesto()
    assert bloqueado and msg.startswith("⛔")


def test_gasto_separa_hoy_y_mes(tmp_control):
    mes_pasado = datetime.now().replace(day=1).isoformat(timespec="seconds")
    registro.COSTOS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(registro.COSTOS_FILE, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": "2000-01-01T00:00:00", "costo_usd": 99}) + "\n")
        fh.write(json.dumps({"ts": mes_pasado, "costo_usd": 1}) + "\n")
    _gastar(0.5)
    g = control.gasto()
    assert round(g["hoy"], 2) in (0.5, 1.5) and round(g["mes"], 2) == 1.5


def test_trabajo_frenado_por_presupuesto(tmp_control, tmp_dirs, monkeypatch):
    monkeypatch.setenv("PRESUPUESTO_MES_USD", "0.01")
    _gastar(0.02)
    monkeypatch.setattr(server, "_opencode_cmd", lambda: "opencode-falso")
    out = run(server.ejecutar_trabajo_opencode_innti("x", "", str(tmp_dirs["workspaces"])))
    assert out.startswith("Freno de presupuesto") and "forzar_presupuesto" in out


# ----------------------------------------------------------------------------- conversación de Antigravity

def test_conversacion_de_agy_por_carpeta(tmp_path, monkeypatch):
    cache = tmp_path / ".gemini" / "antigravity-cli" / "cache"
    cache.mkdir(parents=True)
    carpeta = tmp_path / "personal" / "x"
    (cache / "last_conversations.json").write_text(json.dumps({str(carpeta): "c78222c8-dcb8-4882-9c5c-97e052c3aba0"}))
    monkeypatch.setattr(server.Path, "home", classmethod(lambda cls: tmp_path))
    assert server._agy_conversacion(carpeta) == "c78222c8-dcb8-4882-9c5c-97e052c3aba0"
    assert server._agy_conversacion(tmp_path / "otra") == ""


def test_conversacion_invalida_se_ignora(tmp_dirs, monkeypatch):
    monkeypatch.setattr(server, "_agy_cmd", lambda: "agy")
    ok = run(server.preparar_tarea_antigravity("Corrige", "", "p", "m", conversacion="c78222c8-dcb8-4882-9c5c-97e052c3aba0"))
    assert "--conversation c78222c8-dcb8-4882-9c5c-97e052c3aba0 -i" in ok
    malo = run(server.preparar_tarea_antigravity("Corrige", "", "p2", "m", conversacion="x; rm -rf /"))
    assert "--conversation" not in malo
