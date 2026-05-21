@echo off
setlocal

REM LLMind scripted input runner for xAI Grok with vision hooks forced on.
REM Update these values as needed.
set "LLMIND_PY=C:\Users\Evan\Desktop\LLMind\LLMind\main\LLMind.py"
set "URL=https://api.x.ai/v1/chat/completions"
set "KEY_SLOT=2"
set "MODEL=grok-4.3"

REM Force-enable vision hooks for this run.
set "LLMIND_ENABLE_VISION_HOOKS=1"
set "LLMIND_VISION_MIN_CONFIDENCE=0.72"
set "LLMIND_VISION_MAX_RETRIES=2"
set "LLMIND_ENABLE_LAUNCH_HOOKS=1"
set "LLMIND_ENABLE_UI_HOOKS=1"

(
echo 2
echo %URL%
echo POST
echo %KEY_SLOT%
echo %MODEL%
echo Chrome browser application in windows: Launch Chrome using launch_process. Then navigate to https://x.com/search?q=crypto^&src=typed_query^&f=user using browser_navigation. Wait 3 seconds for the page to load. Then capture the screen and run OCR using capture_and_ocr_screen. Find text People in the OCR blocks, get its center coordinates from the bbox, and click at those coordinates using windows_ui_action action=click. Then capture the screen again with OCR, find text Crypto.com, get its center coordinates, and click using windows_ui_action. Then capture the screen again with OCR, find text Affiliates, get its center coordinates, and click using windows_ui_action.
echo n
echo.
echo.
echo.
echo q
) | python "%LLMIND_PY%"

endlocal
