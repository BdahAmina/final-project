# Start the HTTP API on this computer only (http://127.0.0.1:8000, docs at /docs).
# Usage, from the medical_visit_mvp folder:  powershell -ExecutionPolicy Bypass -File deployment\run_api.ps1
$ErrorActionPreference = "Stop"
$project = Split-Path -Parent $PSScriptRoot
$python = Join-Path (Split-Path -Parent $project) "venv\Scripts\python.exe"
if (-not (Test-Path $python)) { $python = Join-Path $project "venv\Scripts\python.exe" }
if (-not (Test-Path $python)) { throw "venv not found next to or inside the project folder." }
Set-Location $project
& $python -m backend.manage migrate
& $python -m uvicorn backend.api:app --host 127.0.0.1 --port 8000
