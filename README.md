# Orquestador Claude

Servidor MCP que convierte a Claude en el **cerebro** de un equipo de agentes: clasifica cada pedido, lo delega al subagente que corresponde, **revisa** el resultado, vuelve a delegar correcciones y lleva un checklist con costos. Funciona en **Windows y macOS** (y Linux).

| Contexto | Carpeta (configurable) | Subagente |
|---|---|---|
| Trabajo | `~/workspaces` | [OpenCode](https://opencode.ai) con los modelos de tu proveedor (p. ej. un proxy LiteLLM) |
| Todo lo demás (personal, universidad, el propio orquestador) | `~/personal`, `~/universidad`, `OTRAS_CARPETAS_PERSONALES` | [Antigravity](https://antigravity.google) mediante su CLI oficial `agy` |

## Instalación en una línea

Pega la URL del repo a Claude Code con la frase:

> **instala esto siguiendo INSTALL_PARA_CLAUDE.md: https://github.com/iamsebas376/orquestador-claude**

Claude clonará el repositorio, creará el entorno virtual, registrará el servidor MCP en Claude Code y Claude Desktop, instalará las reglas en `~/.claude/CLAUDE.md` sin pisar tu contenido y te dirá el diagnóstico final (ver [`INSTALL_PARA_CLAUDE.md`](INSTALL_PARA_CLAUDE.md)).

## Qué hace

- **Enrutamiento por carpeta:** cada herramienta rechaza rutas del otro contexto.
- **Pestañas integradas de Claude Code:** cada tarea se ve en vivo en una pestaña de terminal de Claude: la TUI de OpenCode pegada a su sesión y el CLI de Antigravity. Claude las abre con `run_in_terminal` y los comandos que le devuelven `preparar_vista_opencode` y `preparar_tarea_antigravity` (generados en PowerShell para Windows o sintaxis POSIX para macOS/Linux); varias en paralelo, una pestaña cada una.
- **Sin avisos de permisos en Antigravity:** el CLI corre con `--dangerously-skip-permissions` (solo en carpetas personales y de universidad); pon `ANTIGRAVITY_SIN_PERMISOS=0` si prefieres que pregunte. A Claude solo vuelve una síntesis.
- **Checklist por pedido** (`datos/tareas/`), con estados, rondas de corrección (límite configurable) y notas de revisión.
- **Correcciones con memoria:** OpenCode reutiliza la sesión y Antigravity la conversación.
- **Respaldo de modelos:** si un modelo de Antigravity llega a su límite, se reintenta solo con el siguiente de `ANTIGRAVITY_CADENA`.
- **Corte por inactividad:** una tarea de OpenCode sin ninguna línea de salida durante 10 minutos se corta (`OPENCODE_IDLE_SECONDS`); la de Antigravity se corta por tiempo total.
- **Costos:** tokens reales de OpenCode × tu tabla de precios (`precios.json`); Antigravity y Claude se registran como equivalente por API.
- **Aprendizajes por proyecto:** qué modelo funcionó bien o mal, para elegir mejor la próxima vez.
- **Verificación por proyecto:** detecta y corre tests/lint (Maven, Gradle, npm, pytest, Go) o los comandos que configures. Antigravity no ejecuta comandos, así que los tests los corre Claude con `verificar_proyecto`.
- **Presupuesto:** límite diario y mensual de gasto en el proveedor, con aviso y freno.
- **Seguridad:** el servidor de OpenCode exige contraseña. En `~/.config/opencode/opencode.jsonc` el bloque `permission.bash` niega borrar archivos (`rm`, `del`, `Remove-Item`…) y los comandos git destructivos (`reset --hard`, `clean`, `push`, `checkout --`…); fuera de la carpeta del proyecto OpenCode ya queda bloqueado. Antigravity, en cambio, corre con `--dangerously-skip-permissions` solo en carpetas personales.

## Requisitos

- Python 3.10+ (o `uv`)
- [Claude Desktop](https://claude.ai/download) (pestañas Chat y Code) o Claude Code CLI
- OpenCode con tu proveedor configurado en `~/.config/opencode/opencode.jsonc`
- Antigravity instalado y con sesión iniciada (crea `~/.gemini/bin/agy` en macOS o `~/.gemini/bin/agy.exe` en Windows)

## Instalación manual

**En macOS / Linux (zsh / bash):**

```bash
git clone https://github.com/iamsebas376/orquestador-claude
cd orquestador-claude
./mcp_orchestrator/setup.sh --yes
```

**En Windows (PowerShell):**

```powershell
git clone https://github.com/iamsebas376/orquestador-claude
cd orquestador-claude
powershell -ExecutionPolicy Bypass -File mcp_orchestrator\setup.ps1 -InstallOpenCode
```

El instalador (`mcp_orchestrator/instalar.py`) es multiplataforma e idempotente: crea el entorno virtual (con `uv` si existe o `venv`), el `.env` (con una contraseña aleatoria para el servidor de OpenCode), registra el servidor en Claude Desktop (`~/Library/Application Support/Claude/claude_desktop_config.json` en macOS o `%APPDATA%\Claude\claude_desktop_config.json` en Windows) y en Claude Code (`~/.claude.json`), actualiza el bloque de reglas en `~/.claude/CLAUDE.md` entre marcadores sin pisar tus notas, instala el hook en `~/.claude/settings.json` e imprime un diagnóstico de qué quedó listo y qué falta.

También puedes consultar solo el diagnóstico o simular los cambios:

```bash
python3 mcp_orchestrator/instalar.py --solo-diagnostico
python3 mcp_orchestrator/instalar.py --dry-run
```

Después de instalar:

1. Revisa `mcp_orchestrator/.env`: carpetas (`TRABAJO_DIR`, `PERSONAL_DIR`, `UNIVERSIDAD_DIR`), modelo por defecto y, si usas el panel, `PANEL_URL`.
2. Revisa `mcp_orchestrator/precios.json` (se crea automáticamente desde `precios.example.json`) si quieres ajustar precios de modelos.
3. Si usas un Proyecto en Claude Chat, pega el contenido de `config/PROJECT_INSTRUCTIONS.md` en sus instrucciones.
4. Reinicia Claude Code / Claude Desktop.

Si mueves la carpeta del proyecto, vuelve a correr el instalador (registra la nueva ruta).

## Configuración (`mcp_orchestrator/.env`)

Lo que quede vacío usa el valor por defecto. Las variables principales:

| Variable | Para qué |
|---|---|
| `TRABAJO_DIR`, `PERSONAL_DIR`, `UNIVERSIDAD_DIR`, `OTRAS_CARPETAS_PERSONALES` | Enrutamiento por carpeta (por defecto `~/workspaces`, `~/personal`, `~/universidad`) |
| `OPENCODE_CLI_PATH`, `INNTI_PROVIDER`, `INNTI_DEFAULT_MODEL` | OpenCode y modelo por defecto |
| `ANTIGRAVITY_AGY_MODEL`, `ANTIGRAVITY_CADENA` | Modelo por defecto de Antigravity y orden de respaldo |
| `ANTIGRAVITY_SIN_PERMISOS` | Aprobar todo en el CLI de Antigravity (1) o que pregunte (0) |
| `OPENCODE_IDLE_SECONDS`, `*_TIMEOUT_SECONDS` | Cortes por inactividad y por tiempo total |
| `PRESUPUESTO_DIA_USD`, `PRESUPUESTO_MES_USD`, `PRESUPUESTO_AVISO_PCT` | Presupuesto de Innti |
| `MAX_RONDAS_CORRECCION` | Rondas de corrección por ítem antes de consultarte |
| `PANEL_URL`, `PANEL_DIAS`, `PANEL_INCLUIR_ARCHIVADAS` | Panel de control |

## Panel de control (opcional)

Publica `config/panel.html` como artefacto en claude.ai con la capacidad `db` (pídeselo a Claude) y pon su URL en `PANEL_URL`. Claude llama `exportar_panel()` al cerrar cada pedido y sincroniza los datos.

- **Qué muestra:** las sesiones de Claude activas (no archivadas ni eliminadas, según la app), tengan o no pedidos: las que solo usan Claude aparecen con su gasto y sin tareas. Las continuaciones de una sesión se unen en una. Con `PANEL_INCLUIR_ARCHIVADAS=1` también salen las archivadas.
- **Tarjetas y gráfica:** estado y revisiones, costo de Innti (real), Antigravity y Claude (equivalente por API), total y el porcentaje del gasto que se quedó en Claude. Usan el mismo alcance que la tabla (todas las sesiones activas).
- **Tabla de sesiones:** gasto por agente y reparto. Al elegir una sesión se ven sus tareas; las de un solo ítem van juntas en «Otras tareas».
- **Filtros:** por período (hoy, 7 y 30 días, todo o fechas) y por IA. El período usa la fecha de la última actividad real (gasto registrado), no la de la última edición.
- **Datos:** se leen de los transcripts de Claude Code (`~/.claude/projects`) y de los archivos de la app de Claude (`CLAUDE_APP_DIR`, por defecto `~/Library/Application Support/Claude` en macOS o `%LOCALAPPDATA%\Claude-Data` en Windows).

## Hook opcional (Claude Code)

El instalador agrega automáticamente en `~/.claude/settings.json`:

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
    }]
  }
}
```

- **`PostToolUse`:** recuerda a Claude sincronizar el panel al cerrar un pedido.

## Herramientas MCP

| Herramienta | Para qué |
|---|---|
| `preparar_vista_opencode` → `ejecutar_trabajo_opencode_innti` / `delegar_rapido` | Tarea de trabajo en OpenCode con un modelo Innti (la pestaña muestra su TUI) |
| `preparar_tarea_antigravity` → `esperar_resultado_antigravity` | Tarea personal en el CLI de Antigravity, en una pestaña |
| `iniciar_pedido`, `actualizar_item`, `agregar_items`, `cerrar_pedido`, `ver_pedido`, `listar_pedidos` | Checklist |
| `verificar_proyecto`, `configurar_verificacion`, `verificar_en_segundo_plano`, `resultado_verificacion` | Tests/lint en cada revisión |
| `definir_reglas_proyecto`, `recordar_regla`, `listar_reglas_proyecto`, `olvidar_regla` | Memoria de reglas por proyecto |
| `resumen_costos`, `registrar_aprendizaje`, `consultar_aprendizajes`, `exportar_panel` | Control |

## Estructura

```
mcp_orchestrator/   código del servidor (server.py, plataforma.py, instalar.py, pedidos.py, costos.py, panel_datos.py, control.py), tests/ y .env
config/             plantillas para Claude (CLAUDE.example.md, PROJECT_INSTRUCTIONS.md), panel.html y orquestador.html
tools/opencode/     OpenCode instalado localmente (opcional)
datos/              lo que genera el orquestador: tareas/, logs/, aprendizajes.jsonl, verificacion.json (no va a git)
```

## Desarrollo

```bash
cd mcp_orchestrator
# macOS / Linux:
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests
# Windows:
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.venv\Scripts\python.exe -m pytest tests
```

`plataforma.py` concentra la detección de SO, rutas, búsqueda de binarios y generación de comandos. `probar.py` ejecuta cualquier herramienta sin cliente MCP: `python probar.py resumen_costos '{"dias": 1}'`.
