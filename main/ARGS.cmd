@echo off
set "SCRIPT_DIR=%~dp0"
set "REPO_ROOT=%SCRIPT_DIR%.."
set "PYTHON_EXE=%PYTHON_EXE%"
if "%PYTHON_EXE%"=="" set "PYTHON_EXE=python"

(
echo 2
echo https://api.x.ai/v1/chat/completions
echo POST
echo 2
echo grok-4.3
echo Open 4 windows notepads and arrange them visibly on the screen.
echo.
echo.
echo.
echo q
) | "%PYTHON_EXE%" "%REPO_ROOT%\main\LLMind.py"
