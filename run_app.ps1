[CmdletBinding()]
param(
    [string]$Date,
    [string]$Start = "00:00",
    [int]$BackendPort = 8001
)

$ErrorActionPreference = "Stop"
$UseLLM = $false
$root = (Resolve-Path $PSScriptRoot).Path
$venvPython = Join-Path $root ".venv\Scripts\python.exe"
$frontend = Join-Path $root "frontend"

if (-not (Test-Path $venvPython)) {
    throw "Python virtual environment not found. Create it with: python -m venv .venv"
}

if (-not (Get-Command node -ErrorAction SilentlyContinue)) {
    throw "Node.js is not installed or not on PATH."
}

if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
    throw "npm is not installed or not on PATH."
}

if (-not (Test-Path (Join-Path $frontend "node_modules"))) {
    Write-Host "Installing frontend dependencies..."
    Push-Location $frontend
    try {
        npm install
        if ($LASTEXITCODE -ne 0) {
            throw "npm install failed."
        }
    }
    finally {
        Pop-Location
    }
}

$dateArgs = if ([string]::IsNullOrWhiteSpace($Date)) { "" } else { "--date `"$Date`"" }
if ($UseLLM) {
    $backendCommand = "Set-Location `"$root`"; & `"$venvPython`" -m backend --start `"$Start`" --host 127.0.0.1 --port $BackendPort $dateArgs"
}
else {
    $backendCommand = "Set-Location `"$root`"; & `"$venvPython`" -m backend --no-llm --start `"$Start`" --host 127.0.0.1 --port $BackendPort $dateArgs"
}
$frontendCommand = "`$env:API_URL = `"http://127.0.0.1:$BackendPort`"; Set-Location `"$frontend`"; npm run dev:api"

Start-Process powershell.exe -ArgumentList @(
    "-NoExit",
    "-ExecutionPolicy", "Bypass",
    "-Command", $backendCommand
)

Start-Process powershell.exe -ArgumentList @(
    "-NoExit",
    "-ExecutionPolicy", "Bypass",
    "-Command", $frontendCommand
)

Write-Host "Backend:  http://127.0.0.1:$BackendPort"
Write-Host "Frontend: http://localhost:5173"
Write-Host "Close the two PowerShell windows to stop the app."
