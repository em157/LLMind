# LLMind scripted input runner for xAI Grok with vision hooks
$LLMIND_PY = "C:\Users\Evan\Desktop\LLMind\LLMind\main\LLMind.py"
$URL = "https://api.x.ai/v1/chat/completions"
$KEY_SLOT = "2"
$MODEL = "grok-4.3"

$TASK_PROMPT = "Task: Automate the following steps using only tool calls: 1. Download a nature image directly from https://picsum.photos/800/600 to C:/Users/Evan/AppData/Local/Temp/nature.jpg. 2. Launch Microsoft Paint. 3. Open the downloaded image file in Paint. 4. Confirm the image is visible in Paint using screen capture and OCR. Do not stop after any step; the workflow is only complete when the image is open and clearly visible in Paint. If any step fails, retry with alternative approach or different URL up to 2 times, then stop and return blocked_reason."

$AGENT_RESPONSES = "Grok tool protocol: use function calls only, one action at a time, with schema-valid arguments. For every capture_and_ocr_screen call, set ocr_engine to winrt. Wait for each function response before the next action. Do not invent tool results or output plain text. Continue calling tools until the image is opened in Paint and visible, or until blocked_reason is returned. Retry verification at most 2 times, then stop with blocked_reason. Final response must include status and summary JSON fields: completed_steps, blocked_reason, confidence, evidence_found."

$PYTHON_EXE = "C:\Users\Evan\Desktop\LLMind\LLMind\.venv-1\Scripts\python.exe"
if (-not (Test-Path $PYTHON_EXE)) {
    $PYTHON_EXE = "python"
}

# Set environment variables for the process
$env:LLMIND_ENABLE_VISION_HOOKS = "1"
$env:LLMIND_VISION_MIN_CONFIDENCE = "0.72"
$env:LLMIND_VISION_MAX_RETRIES = "2"
$env:LLMIND_ENABLE_LAUNCH_HOOKS = "1"
$env:LLMIND_ENABLE_UI_HOOKS = "1"

Write-Host "[LLMind] Running xAI Grok scripted flow"
Write-Host "[LLMind] Model: $MODEL"
Write-Host "[LLMind] URL: $URL"
Write-Host "[LLMind] Key slot: $KEY_SLOT"

$input_data = @(
    "2"
    $URL
    "POST"
    $KEY_SLOT
    $MODEL
    $TASK_PROMPT
    "n"
    $AGENT_RESPONSES
    ""
    ""
    "q"
) | Out-String

$input_data | & $PYTHON_EXE $LLMIND_PY
