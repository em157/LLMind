@echo off
setlocal

set "SCRIPT_DIR=%~dp0"
set "LAUNCHER=%SCRIPT_DIR%LLMind_ContextMenu.cmd"
set "KEY_ROOT=HKCU\Software\Classes\*\shell\LLMindPrompt"

if not exist "%LAUNCHER%" (
  echo Launcher not found: %LAUNCHER%
  exit /b 1
)

reg add "%KEY_ROOT%" /ve /d "Prompt with LLMind" /f >nul
reg add "%KEY_ROOT%" /v "Icon" /d "%SystemRoot%\System32\shell32.dll,70" /f >nul
reg add "%KEY_ROOT%\command" /ve /d "\"%LAUNCHER%\" \"%%1\"" /f >nul

if errorlevel 1 (
  echo Failed to install context menu entry.
  exit /b 1
)

echo Installed: Right click any file - Prompt with LLMind
echo Command: "%LAUNCHER%" "%%1"
exit /b 0
