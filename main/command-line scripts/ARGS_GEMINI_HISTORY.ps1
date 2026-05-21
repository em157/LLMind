# PowerShell wrapper for Chrome history analysis
# This avoids CMD's space-handling issues with the Chrome History path

$chromeHistoryPath = "C:\Users\Evan\AppData\Local\Google\Chrome\User Data\Default\History"
$pythonScript = "C:\Users\Evan\Desktop\LLMind\LLMind\main\LLMind.py"

# Prepare input
$input = @(
    "2",
    "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent",
    "POST",
    "3",
    "gemini-2.5-flash",
    "Analyze Chrome browser history for May 17-18 2026. Generate a comprehensive report summarizing: most visited domains, total visits per day, top 10 pages by frequency. Format clearly and save to chrome_history_report.txt in the temp folder, then open in notepad.exe.",
    "y",
    "1",
    $chromeHistoryPath,
    "2",
    "",
    "",
    "",
    "q"
) | Out-String

# Run Python with piped input
$input | python $pythonScript
