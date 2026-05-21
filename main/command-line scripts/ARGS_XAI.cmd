@echo off
setlocal

REM LLMind scripted input runner for xAI Grok with vision hooks forced on.
set "LLMIND_PY=C:\Users\Evan\Desktop\LLMind\LLMind\main\LLMind.py"
set "URL=https://api.x.ai/v1/chat/completions"
set "KEY_SLOT=2"
set "MODEL=grok-4.3"

REM User request content with strict done criteria to reduce early tool-call stoppage.
set "TASK_PROMPT=Task: Automate the following steps using only tool calls: 1. Download a nature image directly from https://picsum.photos/800/600 to C:/Users/Evan/AppData/Local/Temp/nature.jpg. 2. Launch Microsoft Paint with the image using launch_process app=paint args=[\"C:/Users/Evan/AppData/Local/Temp/nature.jpg\"]. 3. After 2 seconds, call windows_ui_action action=activate_window title_contains=Paint to bring Paint to the foreground. 4. Then call capture_and_ocr_screen with ocr_engine=winrt and region={\"x\":0,\"y\":0,\"width\":1920,\"height\":1080}. Look for OCR text containing Paint, nature, File, Home, or View to confirm success. Do not stop until Paint is confirmed visible. Retry up to 2 times on failure, then return blocked_reason."
set "AGENT_RESPONSES_MODE=1"

REM System instruction content: xAI tool-calling protocol for this runner.
set "AGENT_RESPONSES=Grok tool protocol: use function calls only, one action at a time, with schema-valid arguments. For every capture_and_ocr_screen call, set ocr_engine to winrt and always include region={\"x\":0,\"y\":0,\"width\":1920,\"height\":1080} to capture only the primary monitor. Wait for each function response before the next action. Do not invent tool results or output plain text. When evaluating OCR results, only consider blocks with bbox x ^< 1920 as valid evidence; ignore any blocks at x ^>= 1920 as they come from secondary monitors or editor windows. Continue calling tools until Paint with the image is confirmed visible on the primary monitor, or until blocked_reason is returned. Retry verification at most 2 times, then stop with blocked_reason. Final response must include status and summary JSON fields: completed_steps, blocked_reason, confidence, evidence_found."
if "%AGENT_RESPONSES_MODE%"=="2" set "AGENT_RESPONSES=Grok tool protocol strict use function calls only while executing and return one final status line at completion or block."
if "%AGENT_RESPONSES_MODE%"=="3" set "AGENT_RESPONSES=Grok cautious mode function calls one step at a time with validation gate before click or type and verification after each action."
REM Force-enable vision hooks for this run.
set "LLMIND_ENABLE_VISION_HOOKS=1"
set "LLMIND_VISION_MIN_CONFIDENCE=0.72"
set "LLMIND_VISION_MAX_RETRIES=2"
set "LLMIND_ENABLE_LAUNCH_HOOKS=1"
set "LLMIND_ENABLE_UI_HOOKS=1"

REM Prefer virtualenv python if present, otherwise fall back to system python.
set "PYTHON_EXE=C:\Users\Evan\Desktop\LLMind\LLMind\.venv-1\Scripts\python.exe"
if not exist "%PYTHON_EXE%" set "PYTHON_EXE=python"

echo [LLMind] Running xAI Grok scripted flow
echo [LLMind] Model: %MODEL%
echo [LLMind] URL: %URL%
echo [LLMind] Key slot: %KEY_SLOT%

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