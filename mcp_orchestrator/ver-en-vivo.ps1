# Seguimiento en vivo: muestra en tiempo real lo que hacen los agentes (OpenCode y Antigravity).
# Uso: powershell -NoProfile -File ver-en-vivo.ps1
$log = Join-Path (Split-Path $PSScriptRoot -Parent) 'datos\logs\en-vivo.log'
New-Item -ItemType File -Force -Path $log | Out-Null
[Console]::OutputEncoding = [Text.Encoding]::UTF8
Write-Host "Siguiendo $log (Ctrl+C para salir)" -ForegroundColor DarkGray
Get-Content $log -Wait -Tail 30 -Encoding UTF8
