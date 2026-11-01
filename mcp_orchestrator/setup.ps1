# Instalación del Orquestador MCP en Windows (envoltorio fino hacia instalar.py)
# Uso (desde la carpeta del repo):
#   powershell -ExecutionPolicy Bypass -File mcp_orchestrator\setup.ps1 [-InstallOpenCode] [-SkipRegister] [-DryRun] [-Diagnostico]
param(
    [switch]$InstallOpenCode,   # instala OpenCode localmente en tools\ (sirve incluso con Node antiguo)
    [switch]$SkipRegister,      # no registrar en Claude Desktop / Claude Code
    [switch]$DryRun,            # modo simulación sin escribir cambios
    [switch]$Diagnostico        # solo diagnóstico
)
$ErrorActionPreference = 'Stop'

$App  = $PSScriptRoot
$Root = Split-Path $App -Parent
$Venv = Join-Path $App '.venv'
$Py   = Join-Path $Venv 'Scripts\python.exe'

if ($InstallOpenCode -and -not $DryRun -and -not $Diagnostico) {
    Write-Host '==> OpenCode (instalación local en tools\opencode)' -ForegroundColor Cyan
    $OcDir = Join-Path $Root 'tools\opencode'
    $OcExe = Join-Path $OcDir 'node_modules\opencode-ai\node_modules\opencode-windows-x64\bin\opencode.exe'
    if (-not (Test-Path $OcExe)) {
        New-Item -ItemType Directory -Force -Path $OcDir | Out-Null
        npm install opencode-ai --ignore-scripts --no-save --prefix $OcDir
    }
}

# Localizar Python del venv o del sistema para delegar todo a instalar.py
if (Test-Path $Py) {
    $Runner = $Py
    $Extra  = @()
} elseif (Get-Command py -ErrorAction SilentlyContinue) {
    $Runner = (Get-Command py).Source
    $Extra  = @('-3')
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    $Runner = (Get-Command python).Source
    $Extra  = @()
} else {
    throw 'No se encontró Python 3.10+. Instálalo desde python.org o winget install Python.Python.3.12'
}

$installArgs = @() + $Extra + @((Join-Path $App 'instalar.py'))
if ($SkipRegister) { $installArgs += '--sin-registro' }
if ($DryRun)       { $installArgs += '--dry-run' }
if ($Diagnostico)  { $installArgs += '--solo-diagnostico' }

& $Runner @installArgs
exit $LASTEXITCODE
