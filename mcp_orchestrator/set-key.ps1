# Guarda la API key de Antigravity (Gemini) en .env sin que aparezca en pantalla ni en el historial.
# Uso:  powershell -ExecutionPolicy Bypass -File D:\claude\mcp_orchestrator\set-key.ps1
$ErrorActionPreference = 'Stop'
$envFile = Join-Path $PSScriptRoot '.env'

$secure = Read-Host 'Pega tu API key de Google AI Studio' -AsSecureString
$key = [Runtime.InteropServices.Marshal]::PtrToStringBSTR([Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)).Trim()
if (-not $key) { Write-Host 'Clave vacia, no se cambio nada.' -ForegroundColor Yellow; exit 1 }

$lines = Get-Content $envFile -Encoding UTF8 | ForEach-Object { if ($_ -match '^ANTIGRAVITY_API_KEY=') { "ANTIGRAVITY_API_KEY=$key" } else { $_ } }
[IO.File]::WriteAllLines($envFile, $lines, (New-Object Text.UTF8Encoding $false))

Write-Host 'Probando la clave...' -ForegroundColor Cyan
try {
    $r = Invoke-RestMethod -Uri 'https://generativelanguage.googleapis.com/v1beta/openai/models' -Headers @{ Authorization = "Bearer $key" }
    Write-Host "OK: la clave funciona ($($r.data.Count) modelos disponibles). Reinicia Claude Desktop." -ForegroundColor Green
} catch {
    Write-Host "La clave se guardo, pero la prueba fallo: $($_.Exception.Message)" -ForegroundColor Red
}
