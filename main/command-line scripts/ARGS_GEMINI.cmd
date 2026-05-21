@echo off
setlocal

REM LLMind scripted input runner for Google Gemini with vision hooks forced on.
REM Update these values as needed.
set "LLMIND_PY=C:\Users\Evan\Desktop\LLMind\LLMind\main\LLMind.py"
set "URL=https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-pro:generateContent"
set "KEY_SLOT=3"
set "MODEL=gemini-2.5-pro"

REM User request content with strict done criteria to reduce early tool-call stoppage.
set "TASK_PROMPT=Task: Automate the following steps using only tool calls: 1. Open the Chrome web browser and search for a nature image. 2. Locate a valid image URL in the HTML content. 3. Download the image file to the local system. 4. Launch Microsoft Paint and open the downloaded image. 5. Confirm the image is visible in Paint using screen capture and OCR. Do not stop after downloading; the workflow is only complete when the image is open and visible in Paint. If any step is blocked or ambiguous, retry with a different approach up to 2 times, then stop and return blocked_reason."
set "AGENT_RESPONSES_MODE=1"

REM System instruction content: Gemini tool-calling protocol for this runner.
set "AGENT_RESPONSES=Gemini tool protocol: use function calls only, one action at a time, with schema-valid arguments. For every capture_and_ocr_screen call, set ocr_engine to winrt. Wait for each function response before the next action. Do not invent tool results or output plain text. Continue calling tools until the image is opened in Paint and visible, or until blocked_reason is returned. Retry verification at most 2 times, then stop with blocked_reason. Final response must include status and summary JSON fields: completed_steps, blocked_reason, confidence, evidence_found."
if "%AGENT_RESPONSES_MODE%"=="2" set "AGENT_RESPONSES=Gemini tool protocol strict use function calls only while executing and return one final status line at completion or block."
if "%AGENT_RESPONSES_MODE%"=="3" set "AGENT_RESPONSES=Gemini cautious mode function calls one step at a time with validation gate before click or type and verification after each action."
REM Force-enable vision hooks for this run.
set "LLMIND_ENABLE_VISION_HOOKS=1"
set "LLMIND_VISION_MIN_CONFIDENCE=0.72"
set "LLMIND_VISION_MAX_RETRIES=2"
set "LLMIND_ENABLE_LAUNCH_HOOKS=1"
set "LLMIND_ENABLE_UI_HOOKS=1"

REM Prefer virtualenv python if present, otherwise fall back to system python.
set "PYTHON_EXE=C:\Users\Evan\Desktop\LLMind\LLMind\.venv-1\Scripts\python.exe"
if not exist "%PYTHON_EXE%" set "PYTHON_EXE=python"

echo [LLMind] Running Gemini scripted flow
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
