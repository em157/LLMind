@echo off
setlocal
set "SCRIPT_DIR=%~dp0"
set "REPO_ROOT=%SCRIPT_DIR%.."

powershell -NoProfile -ExecutionPolicy Bypass -File "%REPO_ROOT%\scripts\prompt_workflow.ps1" %*
endlocal
