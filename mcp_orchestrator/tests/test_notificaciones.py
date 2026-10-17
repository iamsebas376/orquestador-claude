"""Grupo 3: notificaciones del sistema (se simula cada plataforma, sin mostrar nada)."""
import importlib

import pytest

import plataforma as plataforma_mod


@pytest.fixture
def plat(monkeypatch):
    # conftest reemplaza notificar en el módulo compartido; aquí se prueba la función real.
    p = importlib.reload(plataforma_mod)
    llamadas = []
    monkeypatch.setattr(p.subprocess, "Popen", lambda args, **kw: llamadas.append((args, kw)))
    monkeypatch.delenv("NOTIFICAR", raising=False)
    return p, llamadas


def test_windows_pasa_el_texto_por_entorno_y_escapado(plat, monkeypatch):
    p, llamadas = plat
    monkeypatch.setattr(p, "IS_WINDOWS", True)
    p.notificar("Título <x>", "a & b \"c\"; $(rm)")
    args, kw = llamadas[0]
    assert args[0] == "powershell" and "ToastNotificationManager" in args[-1]
    assert "rm" not in args[-1]  # el mensaje no se interpola en el script
    assert kw["env"]["ORQ_TITULO"] == "Título &lt;x&gt;"
    assert kw["env"]["ORQ_MENSAJE"] == "a &amp; b &quot;c&quot;; $(rm)"


def test_mac_usa_osascript(plat, monkeypatch):
    p, llamadas = plat
    monkeypatch.setattr(p, "IS_WINDOWS", False)
    monkeypatch.setattr(p, "IS_MAC", True)
    p.notificar("Listo", 'dijo "hola"')
    args, _ = llamadas[0]
    assert args[:2] == ["osascript", "-e"] and "display notification" in args[2] and '\\"hola\\"' in args[2]


def test_se_puede_desactivar(plat, monkeypatch):
    p, llamadas = plat
    monkeypatch.setenv("NOTIFICAR", "0")
    p.notificar("x", "y")
    assert llamadas == []
