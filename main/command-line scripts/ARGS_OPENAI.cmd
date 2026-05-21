@echo off
setlocal

REM LLMind scripted input runner for OpenAI with vision hooks forced on.
set "LLMIND_PY=C:\Users\Evan\Desktop\LLMind\LLMind\main\LLMind.py"
set "URL=https://api.openai.com/v1/chat/completions"
set "KEY_SLOT=1"
set "MODEL=gpt-4.1-mini"
set "DOWNLOAD_PATH=%TEMP%\nature.jpg"

REM User request content. Keep this line simple to avoid CMD metacharacter parsing issues.
set "TASK_PROMPT=Download a nature image from a image search website and save it to %DOWNLOAD_PATH%, verify the file exists at %DOWNLOAD_PATH%, launch Paint with %DOWNLOAD_PATH%, activate the Paint window, and verify Paint is visible using OCR before finishing."
set "AGENT_RESPONSES_MODE=1"

REM System instruction content: OpenAI tool-calling protocol for this runner.
set "AGENT_RESPONSES=OpenAI tool protocol: use function calls only, one action at a time, with valid arguments. Wait for each function response before the next action. Do not invent tool results. Do not launch Notepad. The job is complete only when Paint is open with the downloaded image and OCR confirms Paint UI text on screen."
if "%AGENT_RESPONSES_MODE%"=="2" set "AGENT_RESPONSES=OpenAI tool protocol strict use function calls only while executing and return one final status line at completion or block."
if "%AGENT_RESPONSES_MODE%"=="3" set "AGENT_RESPONSES=OpenAI cautious mode function calls one step at a time with validation gate before click or type and verification after each action."

REM Force-enable vision hooks for this run.
set "LLMIND_ENABLE_VISION_HOOKS=1"
set "LLMIND_VISION_MIN_CONFIDENCE=0.72"
set "LLMIND_VISION_MAX_RETRIES=2"
set "LLMIND_ENABLE_LAUNCH_HOOKS=1"
set "LLMIND_ENABLE_UI_HOOKS=1"

REM Prefer virtualenv python if present, otherwise fall back to system python.
set "PYTHON_EXE=C:\Users\Evan\Desktop\LLMind\LLMind\.venv-1\Scripts\python.exe"
if not exist "%PYTHON_EXE%" set "PYTHON_EXE=python"

echo [LLMind] Running OpenAI scripted flow
echo [LLMind] Model input: %MODEL%
echo [LLMind] URL: %URL%
echo [LLMind] Key slot: %KEY_SLOT%
echo [LLMind] Download path: %DOWNLOAD_PATH%

(
echo 2
echo %URL%
echo POST
echo %KEY_SLOT%
echo %MODEL%
echo %TASK_PROMPT%
echo n
echo %AGENT_RESPONSES%
echo.
echo.
echo q
) | "%PYTHON_EXE%" "%LLMIND_PY%"

endlocal