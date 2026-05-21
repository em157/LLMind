@echo off
setlocal

if "%~1"=="" (
  echo No file path was provided.
  exit /b 1
)

set "SCRIPT_DIR=%~dp0"
set "LLMIND_PY=%SCRIPT_DIR%LLMind.py"
set "SELECTED_FILE=%~1"

if not exist "%LLMIND_PY%" (
  echo Could not find LLMind.py at: %LLMIND_PY%
  exit /b 1
)

set "LLMIND_CTX_URL=https://api.x.ai/v1/chat/completions"
set "LLMIND_CTX_METHOD=POST"
set "LLMIND_CTX_MODEL=grok-4.3"

echo.
echo LLMind Right-Click Prompt
echo File: %SELECTED_FILE%
echo.

python "%LLMIND_PY%" --context-file "%SELECTED_FILE%" --url "%LLMIND_CTX_URL%" --method "%LLMIND_CTX_METHOD%" --model "%LLMIND_CTX_MODEL%"
set "EXITCODE=%ERRORLEVEL%"

echo.
if "%EXITCODE%"=="0" (
  echo Request completed.
) else (
  echo Request failed with exit code %EXITCODE%.
)
echo.
pause
exit /b %EXITCODE%
