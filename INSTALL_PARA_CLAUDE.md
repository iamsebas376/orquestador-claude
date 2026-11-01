# Guía de Instalación Automática para Claude (`INSTALL_PARA_CLAUDE.md`)

Este documento está diseñado para ser ejecutado directamente por **Claude Code** (en macOS, Windows o Linux) cuando el usuario pide instalar y configurar este repositorio.

> **Regla crítica de seguridad para Claude:** **NUNCA** pidas al usuario que escriba contraseñas, tokens ni API keys en el chat, y **NUNCA** escribas secretos en archivos rastreados por git. Limítate a crear las plantillas o indicar en qué archivo local (`.env` o `~/.config/opencode/opencode.jsonc`) debe colocarlos el usuario por su cuenta.

---

## Pasos deterministas que debes ejecutar en orden

### 1. Clona en `~/orquestador-claude` si no existe
Comprueba si ya estás dentro de un clon del repositorio (existe `mcp_orchestrator/instalar.py`). Si no estás dentro:
- **macOS / Linux (bash/zsh):**
  ```bash
  if [ ! -d "$HOME/orquestador-claude" ]; then
    git clone https://github.com/iamsebas376/orquestador-claude.git "$HOME/orquestador-claude"
  fi
  cd "$HOME/orquestador-claude"
  ```
- **Windows (PowerShell):**
  ```powershell
  $dest = Join-Path $HOME "orquestador-claude"
  if (-not (Test-Path $dest)) {
    git clone https://github.com/iamsebas376/orquestador-claude.git $dest
  }
  Set-Location $dest
  ```

### 2. Ejecuta `setup.sh` o `setup.ps1` según el sistema operativo
El script creará el entorno virtual `.venv` (usando `uv` si está disponible o `python -m venv`), instalará `requirements.txt`, generará `mcp_orchestrator/.env` con una contraseña aleatoria segura para el servidor local de OpenCode, registrará el servidor MCP `orchestrator` en Claude Code (`~/.claude.json`) y en Claude Desktop, inyectará de forma idempotente el bloque de reglas en `~/.claude/CLAUDE.md` (entre marcadores `<!-- ORQUESTADOR-CLAUDE:INICIO -->` y `<!-- ORQUESTADOR-CLAUDE:FIN -->`, sin borrar nada del usuario) y configurará el hook en `~/.claude/settings.json`.

- **macOS / Linux:**
  ```bash
  chmod +x mcp_orchestrator/setup.sh setup.sh
  ./mcp_orchestrator/setup.sh --yes
  ```
- **Windows:**
  ```powershell
  powershell -ExecutionPolicy Bypass -File mcp_orchestrator\setup.ps1 -InstallOpenCode
  ```

### 3. Verifica con `instalar.py --solo-diagnostico`
Ejecuta el diagnóstico con el Python del entorno virtual recién creado para confirmar que todos los componentes quedaron registrados:

- **macOS / Linux:**
  ```bash
  ./mcp_orchestrator/.venv/bin/python mcp_orchestrator/instalar.py --solo-diagnostico
  ```
- **Windows:**
  ```powershell
  .\mcp_orchestrator\.venv\Scripts\python.exe mcp_orchestrator\instalar.py --solo-diagnostico
  ```

Opcionalmente, corre la suite de pruebas para verificar que todo pasa en este sistema operativo:
- **macOS / Linux:** `./mcp_orchestrator/.venv/bin/python -m pytest mcp_orchestrator/tests -q`
- **Windows:** `.\mcp_orchestrator\.venv\Scripts\python.exe -m pytest mcp_orchestrator\tests -q`

### 4. Pide al usuario solo lo que no puedas hacer tú (login de `agy`, claves y rutas personalizadas)
Revisa la salida del diagnóstico del paso 3:
- Si **OpenCode CLI** no aparece instalado y tienes `npm` o `brew` a mano, puedes instalarlo tú mismo (`npm i -g opencode-ai` o `./mcp_orchestrator/setup.sh --install-opencode`).
- Si faltan elementos que requieren sesión interactiva o secretos corporativos, **no los pidas por chat**. Entrégale al usuario una lista breve con únicamente lo que falte:
  1. **Login de Antigravity (`agy`):** que instale la aplicación Antigravity e inicie sesión una vez (eso crea `~/.gemini/bin/agy` en macOS o `~/.gemini/bin/agy.exe` en Windows).
  2. **Proveedor y clave del proxy Innti / LiteLLM en OpenCode:** que edite personalmente `~/.config/opencode/opencode.jsonc` (o defina las variables de entorno en su `~/.zshrc` / perfil de usuario o en `mcp_orchestrator/.env`, ignorado por git) con la `baseURL` y `apiKey` de su proveedor.
  3. **Carpetas de proyectos (opcional):** por defecto se usan `~/workspaces` (trabajo), `~/personal` y `~/universidad`. Si el usuario usa otras rutas, que las ajuste en `mcp_orchestrator/.env` (`TRABAJO_DIR`, `PERSONAL_DIR`, `UNIVERSIDAD_DIR`) y vuelva a correr `instalar.py`.

### 5. Reinicia Claude Code / Claude Desktop
Indica al usuario que **reinicie Claude Code** (cerrando y abriendo la sesión/terminal) y **Claude Desktop** para que el servidor MCP `orchestrator` y las reglas de `~/.claude/CLAUDE.md` queden activos.

---

## Tabla de errores comunes y solución rápida

| Problema o síntoma | Causa probable | Cómo resolverlo |
|---|---|---|
| `python3: command not found` o versión `< 3.10` en macOS | macOS sin Python moderno en PATH | Ejecuta `./mcp_orchestrator/setup.sh --yes` (usa Homebrew para instalar `python@3.12`) o instala Python 3.12 desde `python.org` / `brew install python@3.12`. |
| `Permission denied: ./mcp_orchestrator/setup.sh` | El archivo perdió el bit de ejecución al descargarse | Ejecuta `chmod +x mcp_orchestrator/setup.sh setup.sh` o corre directamente `python3 mcp_orchestrator/instalar.py`. |
| `No se encontró OpenCode ('opencode')` | OpenCode CLI no está en `PATH` ni en `tools/opencode` | Instala con `npm i -g opencode-ai` (o `brew install opencode` en macOS), o ejecuta `setup.ps1 -InstallOpenCode` / `setup.sh --install-opencode` y verifica `OPENCODE_CLI_PATH` en `mcp_orchestrator/.env`. |
| `No se encontró agy` | Antigravity no se ha instalado o no se ha iniciado sesión | Abre la app Antigravity e inicia sesión una vez para que instale `~/.gemini/bin/agy` (macOS) o `~/.gemini/bin/agy.exe` (Windows). Si está en otra ruta, ponla en `ANTIGRAVITY_CLI_PATH` dentro de `mcp_orchestrator/.env`. |
| El servidor `orchestrator` no aparece en Claude Code / Desktop | No se ha reiniciado el cliente tras registrar el JSON | Cierra completamente Claude Desktop (incluyendo el icono de la bandeja/menú) y reinicia la sesión de Claude Code. Verifica con `instalar.py --solo-diagnostico`. |
| `Rechazado: <ruta> está fuera de TRABAJO_DIR / PERSONAL_DIR` | La carpeta del proyecto está fuera de `~/workspaces`, `~/personal` o `~/universidad` | Edita `TRABAJO_DIR`, `PERSONAL_DIR`, `UNIVERSIDAD_DIR` u `OTRAS_CARPETAS_PERSONALES` en `mcp_orchestrator/.env` y vuelve a ejecutar `instalar.py`. |
| Error de autenticación o conexión con el modelo en OpenCode | Falta configurar el proxy LiteLLM (`INNTI_PROVIDER`) o su clave | Configura el proveedor en `~/.config/opencode/opencode.jsonc` con su URL y variable de entorno de API key (nunca pongas la clave en el chat). |
