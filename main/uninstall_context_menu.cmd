@echo off
setlocal

set "KEY_ROOT=HKCU\Software\Classes\*\shell\LLMindPrompt"

reg delete "%KEY_ROOT%" /f >nul
if errorlevel 1 (
  echo Context menu entry was not found or could not be removed.
  exit /b 1
)

echo Removed: Prompt with LLMind
exit /b 0
