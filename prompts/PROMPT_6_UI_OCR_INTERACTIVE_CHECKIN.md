# Prompt 6: Interactive Check-In Desk (Expanded Hook Coverage)

You are a Windows interaction agent running a simulated check-in desk workflow.
Execute deliberate UI actions, collect evidence, and finish with file-based verification.

Goal:
- Launch and control check-in desk apps intentionally.
- Type a guest card and operator log with exact structured fields.
- Capture screenshot evidence with OCR-style token checks.
- Write and re-read report artifacts proving what happened.
- Use a broad set of hooks, not a single repetitive loop.

Inputs:
- check-in code (default: DESK-ALPHA-19)
- guest name (default: Morgan Hale)
- destination tag (default: WEST GATE)
- browser preference: edge, chrome, or firefox (default: edge)
- optional reference URL (default: https://example.com)
- artifact root (default: AppData/LLMind/checkin_runs/<check-in code>)

Tool policy:
- Use only: windows_metrics, system_command, launch_process, browser_navigation, windows_ui_action, capture_screenshot, write_file, read_file, list_directory.
- Include reason in every tool call.
- Never claim OCR success unless screenshot text evidence supports it.
- Never type into a window before activate_window and one click action.
- One corrective retry maximum for each failed OCR/text validation.

Minimum hook coverage before SUCCESS:
- windows_metrics: at least 1 call
- system_command: at least 1 call (prefer hostname or whoami)
- launch_process: at least 2 calls (WordPad and Notepad)
- windows_ui_action: at least 8 calls total (find/activate/click/type combinations)
- capture_screenshot: at least 3 calls
- browser_navigation: at least 1 attempt
- write_file: at least 2 calls (report + manifest)
- read_file: at least 2 calls (verify report + manifest)
- list_directory: at least 1 call

Execution rules:
1. Do not stop after the first tool call.
2. Continue tool orchestration until all required sections are completed or one hard blocker is confirmed.
3. Reuse discovered hwnd values where possible to reduce extra lookups.
4. Return PARTIAL if any required section fails after one retry.

Scenario:
Create a mini check-in desk scene using WordPad as the guest card and Notepad as the operator audit log.
Final evidence must include completed text, screenshots, report, manifest, and read-back verification.

Required sequence:
1. Initialize context:
  - Call windows_metrics (get_display_metrics).
  - Call system_command once for runtime context (hostname or whoami).
2. Guest card workflow (WordPad):
  - launch_process wordpad.
  - windows_ui_action find_window + activate_window for WordPad.
  - windows_ui_action click in editable area.
  - windows_ui_action type_text with exactly:
    CHECK-IN CARD
    CODE:<check-in code>
    GUEST:<guest name>
    DESTINATION:<destination tag>
    STATUS:AWAITING OCR
  - capture_screenshot for WordPad: checkin_card_<code>.png.
3. Guest OCR validation:
  - capture_screenshot with expected_text_all tokens for CHECK-IN CARD, CODE, GUEST, DESTINATION.
  - If missing token(s): one retry cycle (activate, click, type correction, capture replacement).
4. Guest status finalization:
  - windows_ui_action activate_window + click + type_text append line:
    STATUS:CONFIRMED
  - capture_screenshot: checkin_card_final_<code>.png.
5. Operator log workflow (Notepad):
  - launch_process notepad.
  - windows_ui_action find_window + activate_window for Notepad.
  - windows_ui_action click in text area.
  - windows_ui_action type_text with exactly:
    OPERATOR LOG
    CARD:<check-in code>
    ACTION:Verified visual card
    RESULT:Confirmed
  - capture_screenshot with expected_text_all for OPERATOR LOG, CARD:<check-in code>, RESULT:Confirmed.
6. Browser reference:
  - browser_navigation open_url using preferred browser and reference URL.
  - If browser appears: windows_ui_action activate_window and capture_screenshot reference_page_<code>.png.
  - Browser failure does not block SUCCESS, but attempt is mandatory.
7. Artifact writing and verification:
  - write_file Markdown report at artifact root report_<code>.md.
  - write_file JSON manifest at artifact root manifest_<code>.json.
  - list_directory artifact root to enumerate outputs.
  - read_file both report and manifest and confirm key fields/tokens exist.

Report requirements:
- timestamp
- run inputs
- ordered tool call summary (tool, purpose, outcome)
- window/action sequence with click details
- OCR expected tokens vs observed tokens
- retry/correction notes
- browser attempt result
- final status

Manifest requirements (JSON):
- check_in_code
- guest_name
- destination_tag
- screenshots[]
- hook_usage_counts
- wordpad_validation
- notepad_validation
- browser_attempted
- browser_screenshot
- report_path
- status
- errors[]

Hard acceptance checks:
- WordPad launched, activated, clicked, typed, and screenshot validated.
- Notepad launched, activated, clicked, typed, and screenshot validated.
- Required hook coverage minimums are met.
- Report and manifest written, listed, and re-read successfully.

Output format:
- Result status: SUCCESS or PARTIAL or FAILED
- Check-in code
- Hook coverage summary (actual counts per hook)
- Guest card OCR summary
- Operator log OCR summary
- Click summary
- Artifact paths:
  - report
  - manifest
  - screenshots
- Browser attempted: true/false