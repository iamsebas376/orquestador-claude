# Instalacion del Orquestador MCP en Windows
# Uso (desde la carpeta del repo):
#   powershell -ExecutionPolicy Bypass -File mcp_orchestrator\setup.ps1 [-InstallOpenCode] [-SkipRegister]
param(
    [switch]$InstallOpenCode,   # instala OpenCode localmente en tools\ (sirve incluso con Node antiguo)
    [switch]$SkipRegister       # no registrar en Claude Desktop / Claude Code
)
$ErrorActionPreference = 'Stop'

$App  = $PSScriptRoot
$Root = Split-Path $App -Parent
$Venv = Join-Path $App '.venv'
$Py   = Join-Path $Venv 'Scripts\python.exe'

Write-Host '==> Entorno virtual + dependencias' -ForegroundColor Cyan
if (-not (Test-Path $Py)) {
    $pyLauncher = Get-Command py -ErrorAction SilentlyContinue
    if ($pyLauncher) { & $pyLauncher.Source -3 -m venv $Venv }
    else { & (Get-Command python -ErrorAction Stop).Source -m venv $Venv }
}
& $Py -m pip install --upgrade pip --quiet
& $Py -m pip install -r (Join-Path $App 'requirements.txt') --quiet
& $Py -c "import mcp, httpx, dotenv, rich; print('    dependencias OK')"

Write-Host '==> OpenCode' -ForegroundColor Cyan
# Instalacion local sin postinstall: funciona con Node antiguo y sin permisos de administrador
$OcDir = Join-Path $Root 'tools\opencode'
$OcExe = Join-Path $OcDir 'node_modules\opencode-ai\node_modules\opencode-windows-x64\bin\opencode.exe'
if (Get-Command opencode -ErrorAction SilentlyContinue) {
    Write-Host "    encontrado en el PATH: $((Get-Command opencode).Source)"
} elseif (Test-Path $OcExe) {
    Write-Host "    encontrado: $OcExe  (pon esta ruta en OPENCODE_CLI_PATH del .env)"
} elseif ($InstallOpenCode) {
    New-Item -ItemType Directory -Force -Path $OcDir | Out-Null
    npm install opencode-ai --ignore-scripts --no-save --prefix $OcDir
    & $OcExe --version
    Write-Host "    instalado: pon $OcExe en OPENCODE_CLI_PATH del .env" -ForegroundColor Yellow
} else {
    Write-Host '    NO encontrado. Ejecuta de nuevo con -InstallOpenCode o fija OPENCODE_CLI_PATH en .env' -ForegroundColor Yellow
}

Write-Host '==> Antigravity (agy)' -ForegroundColor Cyan
$Agy = Join-Path $env:USERPROFILE '.gemini\bin\agy.exe'
if (Test-Path $Agy) { Write-Host "    encontrado: $Agy" }
else { Write-Host '    NO encontrado: instala Antigravity e inicia sesion una vez (crea ~\.gemini\bin\agy.exe)' -ForegroundColor Yellow }

$installArgs = @((Join-Path $App 'instalar.py'))
if ($SkipRegister) { $installArgs += '--sin-registro' }
& $Py @installArgs

Write-Host ''
Write-Host 'Listo. Pendiente:' -ForegroundColor Green
Write-Host "  1. Revisa $App\.env (carpetas, modelos, PANEL_URL) y guarda la clave de respaldo con set-key.ps1 si la usas"
Write-Host "  2. Copia config\CLAUDE.example.md a ~\.claude\CLAUDE.md (Code) y config\PROJECT_INSTRUCTIONS.md a tu Proyecto (Chat)"
Write-Host '  3. Reinicia Claude Desktop'
