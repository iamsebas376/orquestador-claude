# Orquestador Claude

Servidor MCP que convierte a Claude en el **cerebro** de un equipo de agentes: clasifica cada pedido, lo delega al subagente que corresponde, **revisa** el resultado, vuelve a delegar correcciones y lleva un checklist con costos. Funciona en **Windows y macOS**.

| Contexto | Carpeta (configurable) | Subagente |
|---|---|---|
| Trabajo | `~/workspaces` | [OpenCode](https://opencode.ai) con los modelos de tu proveedor (p. ej. un proxy LiteLLM) |
| Personal / universidad | `~/personal`, `~/universidad` | [Antigravity](https://antigravity.google) mediante su CLI oficial `agy` |

## Qué hace

- **Enrutamiento por carpeta:** cada herramienta rechaza rutas del otro contexto.
- **CLI oficiales en vivo:** la tarea corre en la TUI de OpenCode o de Antigravity, en una pestaña del panel de terminal de Claude Code o en una ventana externa (Windows Terminal / Terminal.app).
- **Checklist por pedido** (`tareas/`), con estados, rondas de corrección (límite configurable) y notas de revisión.
- **Costos:** tokens reales de OpenCode × tu tabla de precios (`precios.json`); resumen por día, agente y modelo.
- **Aprendizajes por proyecto:** qué modelo funcionó bien o mal, para elegir mejor la próxima vez.
- **Verificación por proyecto:** detecta y corre tests/lint (Maven, Gradle, npm, pytest, Go) o los comandos que configures.
- **Trabajo aislado:** en repos git, OpenCode trabaja en un worktree con rama `orq/…`; al aprobar, los cambios pasan a tu repo en staging, sin commit.
- **Presupuesto:** límite diario y mensual de gasto en el proveedor, con aviso y freno.
- **Correcciones con memoria:** OpenCode reutiliza la sesión y Antigravity la conversación.

### Hook opcional para el panel (Claude Code)

Para que Claude nunca olvide sincronizar el panel al cerrar un pedido, agrega esto a `~/.claude/settings.json` (funciona en Windows y macOS):

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
- **Panel de control:** artefacto de claude.ai con filtros por fecha que Claude sincroniza al cerrar cada pedido (`config/panel.html`).
- **Seguridad:** el servidor de OpenCode exige contraseña; Antigravity solo edita archivos salvo que lo permitas.

## Requisitos

- Python 3.10+
- [Claude Desktop](https://claude.ai/download) (pestañas Chat y Code)
- OpenCode con tu proveedor configurado en `~/.config/opencode/opencode.jsonc`
- Antigravity instalado y con sesión iniciada (crea `~/.gemini/bin/agy`)

## Instalación

**Windows**
```powershell
git clone https://github.com/iamsebas376/orquestador-claude
cd orquestador-claude
powershell -ExecutionPolicy Bypass -File mcp_orchestrator\setup.ps1 -InstallOpenCode
```

**macOS**
```bash
git clone https://github.com/iamsebas376/orquestador-claude
cd orquestador-claude
bash mcp_orchestrator/setup.sh --install-opencode
```

El instalador crea el entorno virtual, el `.env` (con una contraseña aleatoria para el servidor de OpenCode) y registra el servidor en Claude Desktop y en Claude Code. Después:

1. Revisa `mcp_orchestrator/.env`: carpetas, modelo por defecto y, si usas el panel, `PANEL_URL`.
2. Copia `mcp_orchestrator/precios.example.json` como `precios.json` y pon los precios de tus modelos (opcional, para costos).
3. Copia `config/CLAUDE.example.md` a `~/.claude/CLAUDE.md` (reglas para Claude Code) y pega `config/PROJECT_INSTRUCTIONS.md` en las instrucciones de un Proyecto (Chat).
4. Reinicia Claude Desktop.

## Panel de control (opcional)

Publica `config/panel.html` como artefacto en claude.ai con la capacidad `db` (pídeselo a Claude) y pon su URL en `PANEL_URL`. Al cerrar cada pedido, Claude llama `exportar_panel()` y sincroniza los datos.

## Herramientas MCP

| Herramienta | Para qué |
|---|---|
| `ver_configuracion` | Carpetas, modelos y sistema de esta instalación |
| `listar_modelos_innti` | Modelos del proveedor configurado en OpenCode |
| `preparar_vista_opencode` → `ejecutar_trabajo_opencode_innti` | Tarea de trabajo (con TUI en la terminal de Claude Code) |
| `ejecutar_personal_antigravity` / `preparar_tarea_antigravity` → `esperar_resultado_antigravity` | Tarea personal en Antigravity |
| `iniciar_pedido`, `actualizar_item`, `agregar_items`, `cerrar_pedido`, `ver_pedido`, `listar_pedidos` | Checklist |
| `verificar_proyecto`, `configurar_verificacion` | Tests/lint en cada revisión |
| `integrar_trabajo`, `listar_trabajos_aislados` | Aplicar o descartar trabajo aislado |
| `estado_presupuesto`, `resumen_costos`, `registrar_aprendizaje`, `consultar_aprendizajes`, `exportar_panel` | Control |

## Desarrollo

```bash
cd mcp_orchestrator
.venv/bin/python -m pip install -r requirements-dev.txt   # Windows: .venv\Scripts\python.exe
.venv/bin/python -m pytest tests
```

`plataforma.py` concentra lo que depende del sistema operativo (rutas, terminales y sintaxis de PowerShell o zsh). `probar.py` ejecuta cualquier herramienta sin cliente MCP: `python probar.py ver_configuracion`.
