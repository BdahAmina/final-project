# Start the Streamlit screen on this computer only (http://localhost:8501).
# Usage, from the medical_visit_mvp folder:  powershell -ExecutionPolicy Bypass -File deployment\run_ui.ps1
$ErrorActionPreference = "Stop"
$project = Split-Path -Parent $PSScriptRoot
$python = Join-Path (Split-Path -Parent $project) "venv\Scripts\python.exe"
if (-not (Test-Path $python)) { $python = Join-Path $project "venv\Scripts\python.exe" }
if (-not (Test-Path $python)) { throw "venv not found next to or inside the project folder." }
Set-Location $project
& $python -m backend.manage migrate
& $python -m streamlit run app.py --server.address 127.0.0.1 --server.headless true --browser.gatherUsageStats false
