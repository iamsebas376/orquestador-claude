# Orquestador Claude

Servidor MCP que convierte a Claude en el **cerebro** de un equipo de agentes: clasifica cada pedido, lo delega al subagente que corresponde, **revisa** el resultado, vuelve a delegar correcciones y lleva un checklist con costos. Funciona en **Windows**.

| Contexto | Carpeta (configurable) | Subagente |
|---|---|---|
| Trabajo | `~/workspaces` | [OpenCode](https://opencode.ai) con los modelos de tu proveedor (p. ej. un proxy LiteLLM) |
| Todo lo demás (personal, universidad, el propio orquestador) | `~/personal`, `~/universidad`, `OTRAS_CARPETAS_PERSONALES` | [Antigravity](https://antigravity.google) mediante su CLI oficial `agy` |

## Qué hace

- **Enrutamiento por carpeta:** cada herramienta rechaza rutas del otro contexto.
- **Ejecución directa:** la tarea corre en segundo plano (`opencode run` / `agy -p`), sin abrir un CLI por tarea; a Claude solo vuelve una síntesis. Varias delegaciones en el mismo mensaje corren en paralelo.
- **Seguimiento en vivo:** `mcp_orchestrator\ver-en-vivo.ps1` muestra en una sola terminal lo que hace cada agente en tiempo real (herramientas que usa, archivos que toca, "sigue trabajando" cada 20 s), con una etiqueta por agente, ítem y modelo. Con las reglas de ejemplo, Claude abre esa pestaña la primera vez que delega en una sesión.
- **Checklist por pedido** (`datos/tareas/`), con estados, rondas de corrección (límite configurable) y notas de revisión.
- **Correcciones con memoria:** OpenCode reutiliza la sesión y Antigravity la conversación.
- **Respaldo de modelos:** si un modelo de Antigravity llega a su límite, se reintenta solo con el siguiente de `ANTIGRAVITY_CADENA`.
- **Corte por inactividad:** una tarea sin ninguna línea de salida durante 10 minutos se corta (`OPENCODE_IDLE_SECONDS`, `ANTIGRAVITY_IDLE_SECONDS`).
- **Costos:** tokens reales de OpenCode × tu tabla de precios (`precios.json`); Antigravity y Claude se registran como equivalente por API.
- **Aprendizajes por proyecto:** qué modelo funcionó bien o mal, para elegir mejor la próxima vez.
- **Verificación por proyecto:** detecta y corre tests/lint (Maven, Gradle, npm, pytest, Go) o los comandos que configures. Antigravity no ejecuta comandos, así que los tests los corre Claude con `verificar_proyecto`.
- **Presupuesto:** límite diario y mensual de gasto en el proveedor, con aviso y freno.
- **Seguridad:** el servidor de OpenCode exige contraseña; Antigravity solo edita archivos.

## Requisitos

- Python 3.10+
- [Claude Desktop](https://claude.ai/download) (pestañas Chat y Code)
- OpenCode con tu proveedor configurado en `~/.config/opencode/opencode.jsonc`
- Antigravity instalado y con sesión iniciada (crea `~/.gemini/bin/agy.exe`)

## Instalación

```powershell
git clone https://github.com/iamsebas376/orquestador-claude
cd orquestador-claude
powershell -ExecutionPolicy Bypass -File mcp_orchestrator\setup.ps1 -InstallOpenCode
```

El instalador crea el entorno virtual, el `.env` (con una contraseña aleatoria para el servidor de OpenCode) y registra el servidor en Claude Desktop y en Claude Code. Después:

1. Revisa `mcp_orchestrator/.env`: carpetas, modelo por defecto y, si usas el panel, `PANEL_URL`.
2. Copia `mcp_orchestrator/precios.example.json` como `precios.json` y pon los precios de tus modelos (opcional, para costos).
3. Copia `config/CLAUDE.example.md` a `~/.claude/CLAUDE.md` (reglas para Claude Code) y pega `config/PROJECT_INSTRUCTIONS.md` en las instrucciones de un Proyecto (Chat).
4. Reinicia Claude Desktop.

Si mueves la carpeta del proyecto, vuelve a correr el instalador (registra la nueva ruta) y actualiza `OPENCODE_CLI_PATH` en el `.env` y la ruta del hook en `~/.claude/settings.json`.

## Configuración (`mcp_orchestrator/.env`)

Lo que quede vacío usa el valor por defecto. Las variables principales:

| Variable | Para qué |
|---|---|
| `TRABAJO_DIR`, `PERSONAL_DIR`, `UNIVERSIDAD_DIR`, `OTRAS_CARPETAS_PERSONALES` | Enrutamiento por carpeta |
| `OPENCODE_CLI_PATH`, `INNTI_PROVIDER`, `INNTI_DEFAULT_MODEL` | OpenCode y modelo por defecto |
| `ANTIGRAVITY_AGY_MODEL`, `ANTIGRAVITY_CADENA` | Modelo por defecto de Antigravity y orden de respaldo |
| `OPENCODE_IDLE_SECONDS`, `ANTIGRAVITY_IDLE_SECONDS`, `*_TIMEOUT_SECONDS` | Cortes por inactividad y por tiempo total |
| `PRESUPUESTO_DIA_USD`, `PRESUPUESTO_MES_USD`, `PRESUPUESTO_AVISO_PCT` | Presupuesto de Innti |
| `MAX_RONDAS_CORRECCION` | Rondas de corrección por ítem antes de consultarte |
| `PANEL_URL`, `PANEL_DIAS`, `PANEL_INCLUIR_ARCHIVADAS` | Panel de control |

## Panel de control (opcional)

Publica `config/panel.html` como artefacto en claude.ai con la capacidad `db` (pídeselo a Claude) y pon su URL en `PANEL_URL`. Claude llama `exportar_panel()` al final de cada turno con cambios y al cerrar cada pedido, y sincroniza los datos.

- **Qué muestra:** solo las sesiones de Claude activas (no archivadas ni eliminadas, según la app) donde intervino el orquestador, es decir, que tienen pedidos. Las continuaciones de una sesión se unen en una. Con `PANEL_INCLUIR_ARCHIVADAS=1` también salen las archivadas.
- **Tarjetas y gráfica:** estado y revisiones, costo de Innti (real), Antigravity y Claude (equivalente por API), total y el porcentaje del gasto que se quedó en Claude. Usan el mismo alcance que la tabla.
- **Tabla de sesiones:** gasto por agente y reparto. Al elegir una sesión se ven sus tareas; las de un solo ítem van juntas en «Otras tareas».
- **Filtros:** por período (hoy, 7 y 30 días, todo o fechas) y por IA. El período usa la fecha de la última actividad real (gasto registrado), no la de la última edición.
- **Datos:** se leen de los transcripts de Claude Code y de los archivos de la app de Claude (`CLAUDE_APP_DIR`, por defecto `%LOCALAPPDATA%\Claude-Data`).

## Hooks opcionales (Claude Code)

En `~/.claude/settings.json`:

```json
{
  "hooks": {
    "PostToolUse": [{
      "matcher": "mcp__orchestrator__cerrar_pedido",
      "hooks": [{
        "type": "command",
        "command": "echo '{\"hookSpecificOutput\":{\"hookEventName\":\"PostToolUse\",\"additionalContext\":\"Pedido cerrado: ahora llama mcp__orchestrator__exportar_panel y ejecuta tal cual la llamada ArtifactData batch que devuelve, para sincronizar el panel de control.\"}}'",
        "timeout": 10
      }]
    }],
    "PreToolUse": [{
      "matcher": "Edit|Write|MultiEdit|NotebookEdit",
      "hooks": [{
        "type": "command",
        "command": "python \"<ruta del repo>/hooks/bloquear_edicion.py\"",
        "timeout": 10
      }]
    }]
  }
}
```

- **`PostToolUse`:** recuerda a Claude sincronizar el panel al cerrar un pedido.
- **`PreToolUse` (`hooks/bloquear_edicion.py`):** impide que Claude edite o cree archivos; solo delega y revisa. Permite cambios triviales de hasta 3 líneas, su memoria y la carpeta temporal.

## Herramientas MCP

| Herramienta | Para qué |
|---|---|
| `ejecutar_trabajo_opencode_innti` | Tarea de trabajo en OpenCode con un modelo Innti |
| `ejecutar_personal_antigravity` | Tarea personal en Antigravity (`agy -p`; edita archivos, sin comandos) |
| `iniciar_pedido`, `actualizar_item`, `agregar_items`, `cerrar_pedido`, `ver_pedido`, `listar_pedidos` | Checklist |
| `verificar_proyecto`, `configurar_verificacion` | Tests/lint en cada revisión |
| `resumen_costos`, `registrar_aprendizaje`, `consultar_aprendizajes`, `exportar_panel` | Control |

## Estructura

```
mcp_orchestrator/   código del servidor (server.py, pedidos.py, costos.py, panel_datos.py, control.py), tests/ y .env
config/             reglas de ejemplo para Claude, panel.html y la guía orquestador.html
hooks/              hook opcional que impide a Claude editar archivos
tools/opencode/     OpenCode instalado localmente
datos/              lo que genera el orquestador: tareas/, logs/, aprendizajes.jsonl, verificacion.json (no va a git)
```

## Desarrollo

```powershell
cd mcp_orchestrator
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.venv\Scripts\python.exe -m pytest tests
```

`plataforma.py` concentra las rutas y nombres de binarios por defecto. `probar.py` ejecuta cualquier herramienta sin cliente MCP: `python probar.py resumen_costos '{"dias": 1}'`.
