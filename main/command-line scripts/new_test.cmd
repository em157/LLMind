@echo off
setlocal

REM LLMind scripted input runner for xAI Grok 4.3
set "LLMIND_PY=C:\Users\Evan\Desktop\LLMind\LLMind\main\LLMind.py"
set "API_URL=https://api.x.ai/v1/chat/completions"
set "KEY_SLOT=2"
set "MODEL=grok-4.3"

REM Ensure relevant hooks are enabled for this run
set "LLMIND_ENABLE_LAUNCH_HOOKS=1"
set "LLMIND_ENABLE_UI_HOOKS=1"
set "LLMIND_ENABLE_VISION_HOOKS=1"
set "LLMIND_VISION_MIN_CONFIDENCE=0.72"
set "LLMIND_VISION_MAX_RETRIES=2"

(
echo 2
echo %API_URL%
echo POST
echo %KEY_SLOT%
echo %MODEL%
echo Download https://news.google.com/home?hl=en-US&gl=US&ceid=US:en html webpage with tool calls and parse with tool calls. Produce artifact with URLs from parse webpage only. 
echo n
echo.
echo.
echo.
echo q
) | python "%LLMIND_PY%"

endlocal

