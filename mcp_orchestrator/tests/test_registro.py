import json
from datetime import datetime, timedelta

import pytest

import registro
from conftest import pedido_id


@pytest.fixture
def precios(tmp_path, monkeypatch):
    f = tmp_path / "precios.json"
    f.write_text(json.dumps({"mod-a": [1.00, 0.10, 3.00], "mod-b": [0.20, 0.02, 0.50]}), encoding="utf-8")
    monkeypatch.setattr(registro, "PRECIOS_FILE", f)


def test_costo_por_millon_de_tokens(precios):
    # 100.000 de entrada y 5.000 de salida a 1,00 / 3,00 por millón = US$ 0,115
    assert round(registro.costo_innti("proveedor/mod-a", {"input": 100_000, "output": 5_000}), 5) == 0.115


def test_costo_cuenta_razonamiento_como_salida_y_cache_aparte(precios):
    c = registro.costo_innti("mod-b", {"input": 1_000_000, "reasoning": 1_000_000, "cache_read": 1_000_000})
    assert round(c, 4) == round(0.20 + 0.50 + 0.02, 4)


def test_precios_con_vigencias_y_sufijos(tmp_path, monkeypatch):
    f = tmp_path / "precios.json"
    f.write_text(json.dumps({"gemini-4-argon": [
        {"desde": "2000-01-01", "precio": [1.0, 0.1, 5.0]},
        {"desde": "2026-12-01", "precio": [2.0, 0.2, 10.0]}]}), encoding="utf-8")
    monkeypatch.setattr(registro, "PRECIOS_FILE", f)
    tok = {"input": 1_000_000, "cache_read": 1_000_000, "output": 1_000_000}
    assert registro.costo_innti("gemini-4-argon-medium", tok, "2026-11-30T10:00:00") == 1.0 + 0.1 + 5.0
    assert registro.costo_innti("gemini-4-argon-high", tok, "2026-12-01T00:00:00") == 2.0 + 0.2 + 10.0


def test_equivalente_no_cuenta_para_el_presupuesto(tmp_path, monkeypatch):
    import control
    monkeypatch.setattr(registro, "COSTOS_FILE", tmp_path / "costos.jsonl")
    monkeypatch.setattr(registro, "LOGS_DIR", tmp_path)
    registro.registrar_costo("antigravity", "gemini-4-argon-medium", {"input": 10}, 0.0, equivalente=3.5)
    registro.registrar_costo("opencode", "innti-dsp", {"input": 10}, 0.2)
    assert round(control.gasto()["hoy"], 4) == 0.2
    dia = registro.costos_diarios()
    agy = next(d for d in dia if d["agente"] == "antigravity")
    assert agy["costo_equivalente_usd"] == 3.5 and agy["costo_usd"] == 0
    assert "equivalente API" in registro.resumen_costos(1)


def test_costo_modelo_desconocido_es_cero():
    assert registro.costo_innti("modelo-que-no-existe", {"input": 10_000}) == 0.0


def test_ciclo_de_un_pedido(tmp_registro):
    pid = pedido_id(registro.iniciar_pedido("Prueba", ["A", "B"], "ctx"))
    assert (registro.TAREAS_DIR / f"{pid}.md").exists()
    out = registro.actualizar_item(pid, 1, "hecho", "ok")
    assert "1. ✅ A" in out
    assert "progreso: 1/2" in out
    out = registro.cerrar_pedido(pid, "listo")
    assert "**cerrado**" in out and "listo" in out


def test_titulo_sin_rutas(tmp_registro):
    assert registro.separar_ruta(r"Nexus SaaS – gestión de proyectos (D:\personal\prueba)") == \
        ("Nexus SaaS – gestión de proyectos", r"D:\personal\prueba")
    assert registro.separar_ruta("Mi app - /Users/ana/proyectos/app") == ("Mi app", "/Users/ana/proyectos/app")
    assert registro.separar_ruta("Pedido normal (versión 2)") == ("Pedido normal (versión 2)", "")
    assert registro.separar_ruta(r"D:\workspaces\x") == (r"D:\workspaces\x", "")  # solo ruta: no queda vacío
    pid = pedido_id(registro.iniciar_pedido(r"Landing (D:\personal\web)", ["a"]))
    data = json.loads((registro.TAREAS_DIR / f"{pid}.json").read_text(encoding="utf-8"))
    assert data["titulo"] == "Landing" and data["ruta"] == r"D:\personal\web"


def test_limite_de_rondas_avisa_al_pasarse(tmp_registro):
    pid = pedido_id(registro.iniciar_pedido("Rondas", ["A"]))
    assert "Límite" not in registro.actualizar_item(pid, 1, "corrigiendo")
    assert "Límite" not in registro.actualizar_item(pid, 1, "corrigiendo")
    assert "Límite alcanzado" in registro.actualizar_item(pid, 1, "corrigiendo")


def test_errores_de_estado_e_item(tmp_registro):
    pid = pedido_id(registro.iniciar_pedido("Errores", ["A"]))
    assert registro.actualizar_item(pid, 1, "inventado").startswith("Error")
    assert registro.actualizar_item(pid, 5, "hecho").startswith("Error")
    assert registro.actualizar_item("no-existe", 1, "hecho").startswith("Error")


def test_vincular_delegacion_pasa_a_revisando_y_suma_costo(tmp_registro):
    pid = pedido_id(registro.iniciar_pedido("Vínculo", ["A"]))
    msg = registro.vincular_delegacion(pid, 1, "OpenCode", "innti-dsp", 0.5, "Éxito")
    assert "revisando" in msg
    data = json.loads((registro.TAREAS_DIR / f"{pid}.json").read_text(encoding="utf-8"))
    assert data["items"][0]["estado"] == "revisando"
    assert data["items"][0]["costo_usd"] == 0.5
    assert registro.vincular_delegacion("", 0, "x", "y", 0, "z") == ""


def test_costos_diarios_agrupa_por_dia_y_modelo(tmp_registro):
    registro.registrar_costo("opencode", "innti-dsp", {"input": 100, "output": 10}, 0.1)
    registro.registrar_costo("opencode", "innti-dsp", {"input": 50, "reasoning": 5}, 0.2)
    registro.registrar_costo("antigravity", "gemini-3.8-flash-low", {}, 0.0)
    filas = registro.costos_diarios()
    dsp = next(f for f in filas if f["modelo"] == "innti-dsp")
    assert dsp["tareas"] == 2 and dsp["tokens_entrada"] == 150 and dsp["tokens_salida"] == 15
    assert round(dsp["costo_usd"], 4) == 0.3
    assert dsp["dia"] == datetime.now().date().isoformat()


def test_datos_panel_incluye_fechas_y_respeta_tamano(tmp_registro):
    for i in range(3):
        registro.iniciar_pedido(f"Pedido {i}", ["x" * 50] * 5)
    data = registro.datos_panel()
    assert len(data["pedidos"]) == 3
    assert all(p["creado"] for p in data["pedidos"])
    assert "costos_diarios" in data and data["dias"] == registro.PANEL_DIAS
    assert len(json.dumps(data).encode()) <= 240_000


def test_resumen_costos_filtra_por_dias(tmp_registro):
    registro.LOGS_DIR.mkdir(parents=True, exist_ok=True)
    viejo = (datetime.now() - timedelta(days=10)).isoformat(timespec="seconds")
    with open(registro.COSTOS_FILE, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": viejo, "agente": "opencode", "modelo": "innti-dsp", "tokens": {},
                             "costo_usd": 9.0}) + "\n")
    registro.registrar_costo("opencode", "innti-dsf", {}, 0.01)
    assert "US$ 0.0100" in registro.resumen_costos(1)
    assert "US$ 9.0100" in registro.resumen_costos(30)


def test_aprendizajes_por_proyecto(tmp_registro, monkeypatch):
    ws = tmp_registro / "workspaces"
    monkeypatch.setenv("TRABAJO_DIR", str(ws))
    registro.registrar_aprendizaje(str(ws / "ssdd-back" / "src"), "OpenCode", "innti-dsp", "bien", "ok")
    registro.registrar_aprendizaje("otro", "OpenCode", "innti-dsf", "mal", "falló")
    out = registro.consultar_aprendizajes(str(ws / "ssdd-back"))
    assert "ssdd-back" in out and "innti-dsp" in out and "innti-dsf" not in out


def test_consumo_de_claude_en_el_panel(tmp_registro, tmp_path, monkeypatch):
    precios = tmp_path / "precios.json"
    precios.write_text(json.dumps({"claude-x": [5.0, 0.5, 25.0]}), encoding="utf-8")
    monkeypatch.setattr(registro, "PRECIOS_FILE", precios)
    proy = tmp_path / "claude-projects" / "D--x"
    proy.mkdir(parents=True)
    monkeypatch.setattr(registro, "CLAUDE_PROJECTS_DIR", proy.parent)
    pid = pedido_id(registro.iniciar_pedido("Con Claude", ["A"]))
    ts = datetime.now().astimezone().isoformat()
    uso = {"input_tokens": 1_000_000, "cache_read_input_tokens": 1_000_000, "output_tokens": 1_000_000,
           "cache_creation_input_tokens": 1_000_000, "cache_creation": {"ephemeral_1h_input_tokens": 1_000_000}}
    msg = {"timestamp": ts, "sessionId": "abcdef123456", "message": {"id": "msg_1", "model": "claude-x", "usage": uso}}
    # La misma respuesta aparece una vez por bloque de contenido: debe contarse una sola vez.
    (proy / "s.jsonl").write_text("\n".join([json.dumps(msg)] * 3), encoding="utf-8")
    # 5 entrada + 0,5 caché + 25 salida + 2 × 5 escritura de caché de 1 h = 40,5
    data = registro.datos_panel()
    cl = [c for c in data["costos_diarios"] if c["agente"] == "claude"]
    assert len(cl) == 1 and cl[0]["costo_equivalente_usd"] == 40.5 and cl[0]["tareas"] == 1
    assert cl[0]["costo_usd"] == 0
    assert cl[0]["sesiones"] == ["abcdef12"]
    p = next(p for p in data["pedidos"] if p["id"] == pid)
    assert p["claude"]["costo_equivalente_usd"] == 40.5 and p["claude"]["tokens"] == 4_000_000
