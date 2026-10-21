import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import registro  # noqa: E402
import server  # noqa: E402


@pytest.fixture(autouse=True)
def sin_efectos_externos(tmp_path, monkeypatch):
    """Ningún test escribe en el log real ni abre ventanas."""
    monkeypatch.setattr(server, "LIVE_LOG", tmp_path / "en-vivo.log")
    monkeypatch.setattr(server, "SHOW_CONSOLE", False)
    # Ni el registro de pedidos/costos ni la config de agy reales (antes un test creó pedidos de verdad).
    monkeypatch.setattr(registro, "TAREAS_DIR", tmp_path / "tareas-aisladas")
    monkeypatch.setattr(registro, "LOGS_DIR", tmp_path / "logs-aislados")
    monkeypatch.setattr(registro, "COSTOS_FILE", tmp_path / "logs-aislados" / "costos.jsonl")
    monkeypatch.setattr(registro, "APRENDIZAJES_FILE", tmp_path / "aprendizajes-aislados.jsonl")
    monkeypatch.setattr(registro, "CLAUDE_PROJECTS_DIR", tmp_path / "claude-projects-aislado")
    monkeypatch.setattr(server, "AGY_SETTINGS", tmp_path / "agy-settings-aislado.json")
    monkeypatch.setattr(registro, "NOMBRES_FILE", tmp_path / "nombres-aislado.json")


@pytest.fixture
def tmp_registro(tmp_path, monkeypatch):
    """Aísla checklist, costos y aprendizajes en una carpeta temporal."""
    monkeypatch.setattr(registro, "TAREAS_DIR", tmp_path / "tareas")
    monkeypatch.setattr(registro, "LOGS_DIR", tmp_path / "logs")
    monkeypatch.setattr(registro, "COSTOS_FILE", tmp_path / "logs" / "costos.jsonl")
    monkeypatch.setattr(registro, "APRENDIZAJES_FILE", tmp_path / "aprendizajes.jsonl")
    monkeypatch.setattr(registro, "MAX_RONDAS", 2)
    monkeypatch.setattr(registro, "CLAUDE_PROJECTS_DIR", tmp_path / "claude-projects")  # no lee los reales
    return tmp_path


@pytest.fixture
def tmp_dirs(tmp_path, monkeypatch):
    """Carpetas de trabajo/personal/universidad temporales para el servidor."""
    dirs = {k: tmp_path / k for k in ("workspaces", "personal", "universidad", "logs")}
    for d in dirs.values():
        d.mkdir()
    monkeypatch.setattr(server, "TRABAJO_DIR", dirs["workspaces"])
    monkeypatch.setattr(server, "AGY_SETTINGS", tmp_path / "agy-settings.json")  # no toca la config real de agy
    monkeypatch.setattr(server, "PERSONAL_DIR", dirs["personal"])
    monkeypatch.setattr(server, "UNIVERSIDAD_DIR", dirs["universidad"])
    monkeypatch.setattr(server, "LOGS_DIR", dirs["logs"])
    monkeypatch.setattr(server, "LIVE_LOG", dirs["logs"] / "en-vivo.log")
    return dirs


def pedido_id(texto: str) -> str:
    return texto.split("\n")[0].split(": ")[1]
