"""Grupo 2: verificación por proyecto, presupuesto y conversación de Antigravity."""
import asyncio
import json
import sys
from datetime import datetime

import pytest

import control
import registro
import server


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def tmp_control(tmp_path, monkeypatch):
    monkeypatch.setattr(control, "VERIF_FILE", tmp_path / "verificacion.json")
    monkeypatch.setattr(control, "REGLAS_FILE", tmp_path / "reglas.json")
    monkeypatch.setattr(control, "REGLAS_CANDIDATAS_FILE", tmp_path / "reglas_candidatas.json")
    monkeypatch.setattr(registro, "COSTOS_FILE", tmp_path / "costos.jsonl")
    for v in ("PRESUPUESTO_DIA_USD", "PRESUPUESTO_MES_USD", "PRESUPUESTO_AVISO_PCT"):
        monkeypatch.delenv(v, raising=False)
    return tmp_path


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


def test_tokens_de_agy_desde_el_transcript(tmp_path, monkeypatch):
    conv = "c78222c8-dcb8-4882-9c5c-97e052c3aba0"
    logs = tmp_path / ".gemini" / "antigravity-cli" / "brain" / conv / ".system_generated" / "logs"
    logs.mkdir(parents=True)
    filas = [
        {"type": "USER_INPUT", "created_at": "2026-10-17T15:55:40Z"},
        {"type": "PLANNER_RESPONSE", "created_at": "2026-10-17T15:55:50Z", "input_tokens": 100, "output_tokens": 10,
         "cache_read_tokens": 5},
        {"type": "PLANNER_RESPONSE", "created_at": "2026-10-17T16:10:00Z", "input_tokens": 200, "output_tokens": 20,
         "cache_read_tokens": 0},
    ]
    (logs / "transcript_full.jsonl").write_text("\n".join(json.dumps(f) for f in filas), encoding="utf-8")
    monkeypatch.setattr(server.Path, "home", classmethod(lambda cls: tmp_path))
    from datetime import timezone
    todo = server._agy_tokens(conv, datetime(2000, 1, 1, tzinfo=timezone.utc))
    assert todo == {"input": 300, "output": 30, "cache_read": 5}
    # una ronda de corrección posterior solo cuenta sus propias respuestas
    ronda = server._agy_tokens(conv, datetime(2026, 10, 17, 16, 0, tzinfo=timezone.utc))
    assert ronda == {"input": 200, "output": 20, "cache_read": 0}
    assert server._agy_tokens("no-valida; rm", datetime(2000, 1, 1, tzinfo=timezone.utc)) == {}


def test_cierre_personal_registra_equivalente_con_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(registro, "COSTOS_FILE", tmp_path / "costos.jsonl")
    monkeypatch.setattr(registro, "LOGS_DIR", tmp_path)
    precios = tmp_path / "precios.json"
    precios.write_text(json.dumps({"gemini-3.8-flash": [0.375, 0.0375, 1.875]}), encoding="utf-8")
    monkeypatch.setattr(registro, "PRECIOS_FILE", precios)
    res = "Éxito: Antigravity listo.\nTokens: 2.000.000 entrada (1.000.000 de caché) / 1.000.000 salida\n"
    out = server._cierre_personal(res, "", 0, "gemini-3.8-flash-medium", tmp_path)
    fila = json.loads((tmp_path / "costos.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert fila["tokens"] == {"input": 1_000_000, "cache_read": 1_000_000, "output": 1_000_000}
    assert fila["costo_usd"] == 0.0 and fila["costo_equivalente_usd"] == round(0.375 + 0.0375 + 1.875, 6)
    assert "Equivalente por la API de Gemini" in out


def test_conversacion_invalida_se_ignora(tmp_dirs, monkeypatch):
    monkeypatch.setattr(server, "_agy_cmd", lambda: "agy")
    ok = run(server.preparar_tarea_antigravity("Corrige", "", "p", "m", conversacion="c78222c8-dcb8-4882-9c5c-97e052c3aba0"))
    assert "--conversation c78222c8-dcb8-4882-9c5c-97e052c3aba0 -i" in ok
    malo = run(server.preparar_tarea_antigravity("Corrige", "", "p2", "m", conversacion="x; rm -rf /"))
    assert "--conversation" not in malo


# ----------------------------------------------------------------------------- reglas por proyecto y verificación en segundo plano

def test_sin_reglas_por_defecto_y_definir_reglas_proyecto(tmp_control):
    # Por defecto no hay reglas para ningún proyecto
    assert control.reglas_para("ssdd-corporativo-mf") == []

    # Definir reglas nuevas
    out = server.definir_reglas_proyecto("mi-proy", ["Regla 1", "Regla 2"])
    assert "guardadas (2)" in out
    assert control.reglas_para("mi-proy") == ["Regla 1", "Regla 2"]

    # Lista vacía = quitar las reglas
    out_del = server.definir_reglas_proyecto("mi-proy", [])
    assert "eliminadas" in out_del
    assert control.reglas_para("mi-proy") == []


def test_verificar_en_segundo_plano_y_resultado(tmp_control, tmp_dirs):
    proy = tmp_dirs["workspaces"] / "bg-proy"
    proy.mkdir()
    cmd_ok = f'"{sys.executable}" -c "print(\'todo ok\')"'
    server.configurar_verificacion("bg-proy", [cmd_ok])

    inicio = server.verificar_en_segundo_plano("bg-proy")
    assert inicio.startswith("verificacion_id: ")
    vid = inicio.splitlines()[0].split(": ", 1)[1].strip()

    info = control._VERIF_BG[vid]
    info["thread"].join(timeout=10)
    res = server.resultado_verificacion(vid)
    assert res.startswith("Verificación VERDE") and "✅" in res
    assert server.resultado_verificacion("no-existe").startswith("Error:")


# ----------------------------------------------------------------------------- memoria automática de reglas

def test_recordar_regla_conteo_y_rechazo_largo(tmp_control):
    # Rechaza si supera 160 caracteres
    larga = "x" * 161
    res_larga = server.recordar_regla("mi-proy", larga)
    assert "supera el límite de 160 caracteres" in res_larga

    # Primer registro: candidata 1/2
    res = server.recordar_regla("mi-proy", "Usar siempre pytest para los tests")
    assert res == "candidata 1/2"

    # No está aún en reglas fijas
    assert control.reglas_para("mi-proy") == []

    # Está en candidatas con conteo 1 y fecha ISO
    cand_data = control._leer_candidatas()
    assert "mi-proy" in cand_data
    assert len(cand_data["mi-proy"]) == 1
    assert cand_data["mi-proy"][0]["veces"] == 1
    assert "T" in cand_data["mi-proy"][0]["ultima"]


def test_recordar_regla_similitud_y_redaccion_corta(tmp_control):
    # Call 1: redacción más larga
    res1 = server.recordar_regla("mi-proy", "Usar siempre pytest para los tests")
    assert res1 == "candidata 1/2"

    # Call 2: redacción más corta con similitud Jaccard >= 0.7 (diferencias de acentos/signos/palabras vacías)
    res2 = server.recordar_regla("mi-proy", "¡Usar pytest en tests!")
    assert res2 == "promovida a regla fija de mi-proy"

    # Verifica que promovió conservando la redacción más corta
    fijas = control.reglas_para("mi-proy")
    assert fijas == ["¡Usar pytest en tests!"]

    # Ya no está en candidatas
    cand_data = control._leer_candidatas()
    assert "mi-proy" not in cand_data

    # Call 3 con regla similar: detecta que ya existía
    res3 = server.recordar_regla("mi-proy", "usar pytest para tests")
    assert res3 == "ya existía"
    assert control.reglas_para("mi-proy") == ["¡Usar pytest en tests!"]


def test_recordar_regla_tope_15(tmp_control):
    # Llenamos con 15 reglas fijas
    iniciales = [f"Regla fija número {i}" for i in range(1, 16)]
    server.definir_reglas_proyecto("mi-proy", iniciales)
    assert len(control.reglas_para("mi-proy")) == 15

    # Registramos una candidata nueva
    res1 = server.recordar_regla("mi-proy", "Nueva regla especial")
    assert res1 == "candidata 1/2"

    # Segundo registro: debería promover pero choca con el tope de 15
    res2 = server.recordar_regla("mi-proy", "Nueva regla especial")
    assert "Tope alcanzado" in res2 and "15 reglas" in res2
    assert "No se promovió" in res2

    # Verifica que reglas fijas siguen siendo 15
    assert len(control.reglas_para("mi-proy")) == 15

    # Permanece en candidatas
    cands = control._leer_candidatas()
    assert len(cands["mi-proy"]) == 1
    assert cands["mi-proy"][0]["veces"] == 2


def test_olvidar_regla_fija_y_candidata(tmp_control):
    # Creamos una fija y una candidata
    server.definir_reglas_proyecto("mi-proy", ["No tocar templates de scss"])
    server.recordar_regla("mi-proy", "Usar typing estricto en python")

    assert len(control.reglas_para("mi-proy")) == 1
    assert len(control._leer_candidatas().get("mi-proy", [])) == 1

    # Olvidar candidata por similitud
    res_olv_cand = server.olvidar_regla("mi-proy", "usar typing estricto python")
    assert "Regla olvidada" in res_olv_cand and "candidatas" in res_olv_cand
    assert len(control._leer_candidatas().get("mi-proy", [])) == 0

    # Olvidar fija por similitud
    res_olv_fija = server.olvidar_regla("mi-proy", "no tocar templates scss")
    assert "Regla olvidada" in res_olv_fija and "fijas" in res_olv_fija
    assert len(control.reglas_para("mi-proy")) == 0

    # Olvidar algo que no existe
    res_nada = server.olvidar_regla("mi-proy", "regla totalmente desconocida")
    assert "No se encontró" in res_nada


def test_archivo_ausente_o_corrupto(tmp_control, tmp_path):
    # Archivos no existen inicialmente -> lectura devuelve dict vacío
    assert control._leer_reglas() == {}
    assert control._leer_candidatas() == {}

    # Corrompemos el archivo de reglas y el de candidatas
    (tmp_path / "reglas.json").write_text("{corrupto: json invalid!!!", encoding="utf-8")
    (tmp_path / "reglas_candidatas.json").write_text("--- no es json ---", encoding="utf-8")

    assert control._leer_reglas() == {}
    assert control._leer_candidatas() == {}

    # recordar_regla funciona limpiamente a pesar de archivos corruptos
    res1 = server.recordar_regla("mi-proy", "Regla de prueba")
    assert res1 == "candidata 1/2"

    res2 = server.recordar_regla("mi-proy", "Regla de prueba")
    assert res2 == "promovida a regla fija de mi-proy"
    assert control.reglas_para("mi-proy") == ["Regla de prueba"]


def test_listar_reglas_proyecto(tmp_control):
    # Sin reglas
    assert "Sin reglas fijas ni candidatas" in server.listar_reglas_proyecto()
    assert "Sin reglas fijas ni candidatas para 'p1'" in server.listar_reglas_proyecto("p1")

    # Agregamos una fija y una candidata
    server.definir_reglas_proyecto("p1", ["Regla fija A"])
    server.recordar_regla("p1", "Regla candidata B")

    out_p1 = server.listar_reglas_proyecto("p1")
    assert "Reglas fijas:" in out_p1 and "Regla fija A" in out_p1
    assert "Reglas candidatas:" in out_p1 and "Regla candidata B" in out_p1

    out_todos = server.listar_reglas_proyecto()
    assert "Proyecto 'p1':" in out_todos
    assert "Regla fija A" in out_todos
    assert "Regla candidata B" in out_todos
