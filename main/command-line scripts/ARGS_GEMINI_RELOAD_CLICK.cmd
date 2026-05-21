@echo off
setlocal

REM LLMind scripted input runner for Gemini tool-calling prompt.
REM Update these values as needed.
set "LLMIND_PY=C:\Users\Evan\Desktop\LLMind\LLMind\main\LLMind.py"
set "URL=https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"
set "KEY_SLOT=3"
set "MODEL=gemini-2.5-flash"
set "TASK_PROMPT=Identify the browser window (chrome) and OCR the 'reload page button' and click. Rules: use tool calls only; find Chrome window; capture OCR; detect visual candidates for reload button; validate target with validate_click_target; click only when valid=true and confidence^>=0.55; verify UI change; otherwise noop with structured reason/discrimination."

REM Force-enable required hooks for this run.
set "LLMIND_ENABLE_VISION_HOOKS=1"
set "LLMIND_VISION_MIN_CONFIDENCE=0.55"
set "LLMIND_VISION_MAX_RETRIES=2"
set "LLMIND_ENABLE_UI_HOOKS=1"

(
echo 2
echo %URL%
echo POST
echo %KEY_SLOT%
echo %MODEL%
echo %TASK_PROMPT%
echo n
echo.
echo.
echo.
echo q
) | python "%LLMIND_PY%"

endlocal
