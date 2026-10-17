@echo off
setlocal
cd /d "%~dp0"
set "VISIT_PY="
if exist "venv\Scripts\python.exe" set "VISIT_PY=%~dp0venv\Scripts\python.exe"
if not defined VISIT_PY if exist ".venv\Scripts\python.exe" set "VISIT_PY=%~dp0.venv\Scripts\python.exe"
if not defined VISIT_PY if exist "..\venv\Scripts\python.exe" set "VISIT_PY=%~dp0..\venv\Scripts\python.exe"
if not defined VISIT_PY if exist "..\.venv\Scripts\python.exe" set "VISIT_PY=%~dp0..\.venv\Scripts\python.exe"
if defined VISIT_PY goto install
py -m venv venv
if errorlevel 1 goto error
set "VISIT_PY=%~dp0venv\Scripts\python.exe"
:install
"%VISIT_PY%" -m pip install --upgrade -r requirements.txt
if errorlevel 1 goto error
"%VISIT_PY%" -m streamlit run app.py
if errorlevel 1 goto error
exit /b 0
:error
echo Setup or startup failed. See the error above.
pause
exit /b 1
