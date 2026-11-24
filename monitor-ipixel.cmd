@echo off
setlocal
if defined IPIXEL_PYTHON goto run
set "IPIXEL_PYTHON=%~dp0.venv\Scripts\python.exe"
if not exist "%IPIXEL_PYTHON%" set "IPIXEL_PYTHON=python"
:run
"%IPIXEL_PYTHON%" "%~dp0monitor_ipixel.py" %*
exit /b %errorlevel%
