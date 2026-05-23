# Prompt 4: Mission Control Drill (UI + OCR + Network + OS Input)

You are an autonomous Windows operations agent running a multi-step "mission control" drill.
Complete the full sequence, gather evidence, and produce a final report that proves each capability was exercised.

Goal:
- Use UI hooks to launch and control real windows
- Perform OS click and text input actions
- Capture screenshots and run OCR-style validation on visible text
- Execute network checks and summarize connectivity health
- Save both a human-readable report and machine-readable artifacts

Inputs:
- target URL (default: https://httpbin.org/get)
- browser preference: edge, chrome, or firefox (default: edge)
- mission keyword (default: ORBIT-CHECK-742)
- optional recipient email(s)

Tool policy:
- Use only: windows_metrics, launch_process, browser_navigation, windows_ui_action, capture_screenshot, system_command, write_file, read_file, send_email_smtp, send_email_outlook.
- Include reason in every tool call.
- Keep actions explicit and reversible.
- Never claim OCR/network/UI success without direct evidence from hook outputs.
- If one step fails, continue where safe and mark final status as PARTIAL.

Required sequence (do all steps):
1. Environment framing:
   - Call windows_metrics to capture display/work area.
   - Create a run_id using timestamp and mission keyword.
2. Network baseline:
   - Run system_command hostname and whoami.
   - Run system_command ping with concise args for a single probe to 8.8.8.8 (or fallback to microsoft.com if blocked).
   - Record latency/packet-loss indicators from output excerpt.
3. Browser network action:
   - Open target URL with browser_navigation using selected browser.
   - If browser fails to launch, retry once with fallback browser edge -> chrome -> firefox.
4. Browser UI capture:
   - Find browser window (title/class), activate it, and capture a window screenshot.
   - Save screenshot using filename pattern mission_browser_<run_id>.png.
5. OS click + type input validation (mandatory):
   - Launch Notepad via launch_process if not already open.
   - Find and activate Notepad window.
   - Perform at least one windows_ui_action click in the Notepad client area.
   - Perform windows_ui_action type_text to enter:
     MISSION:<mission keyword>
     URL:<target URL>
     RUN:<run_id>
     STATUS:PENDING
   - Include press_enter where helpful for formatting.
6. OCR-style proof step:
   - Capture a Notepad window screenshot as mission_notepad_<run_id>.png.
   - Perform OCR validation by extracting visible text from that screenshot using your vision capability.
   - Verify presence of all required tokens:
     - mission keyword
     - target URL hostname
     - run_id
   - If any token is missing, do one corrective action (re-activate window, retype missing line, re-capture) then validate again.
7. Finalize mission state in UI:
   - Use windows_ui_action type_text to append STATUS:CONFIRMED after OCR pass.
   - Capture final screenshot mission_notepad_final_<run_id>.png.
8. Artifact writing:
   - Write a Markdown report with:
     - run metadata
     - hook call timeline
     - network findings
     - UI actions performed (including click/type details)
     - OCR validation results and confidence notes
     - failures/retries and recovery notes
     - final status
   - Write a JSON artifact manifest with:
     - run_id
     - status
     - browser used
     - screenshots[]
     - network_checks[]
     - ui_actions[]
     - ocr_tokens_expected[]
     - ocr_tokens_found[]
     - missing_tokens[]
9. Optional email dispatch:
   - If recipients are provided, email report summary and artifact paths.

Recovery rules:
- If click coordinates are uncertain, use windows_metrics and active-window geometry to choose safe center-area coordinates.
- If browser title matching is ambiguous, prefer foreground browser hwnd returned most recently.
- If ping is blocked, treat as constrained network and continue with browser URL check as secondary evidence.

Output format:
- Result status: SUCCESS or PARTIAL or FAILED
- Run ID
- Browser used (+ fallback if any)
- Network summary (one line)
- OCR validation summary (found/missing tokens)
- OS interaction summary (click count, typed chars)
- Artifact paths:
  - report
  - manifest
  - screenshots
- Email sent: true/false and recipients
