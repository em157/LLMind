# Computer Vision Adaptation Guide for LLMind (Desktop TL App)

This document is a practical blueprint for adding computer vision to LLMind in a way that is safe, testable, and compatible with the existing hook/tool architecture.

## 1) Goal

Add reliable desktop perception and action support for task-level (TL) automation:
- Read UI state (screen + OCR + optional UIA metadata)
- Let an LLM reason over what is visible
- Execute actions with verification and safety checks

Use deterministic automation first, vision second, and LLM semantics third.

## 2) Design Principles

1. Prefer deterministic controls over pixels
- Use Windows UI Automation (UIA) by control name/id when possible.
- Fall back to CV/OCR only when UIA metadata is missing or unreliable.

2. Keep model outputs structured
- Require strict JSON outputs from vision-capable models.
- Do not execute free-form text instructions directly.

3. Verify every action
- Capture before/after state and check expected change.
- Retry with bounded attempts and alternative strategy.

4. Separate perception, planning, and execution
- Perception: screenshot, OCR, regions, candidates.
- Planning: LLM decision with explicit confidence.
- Execution: safe hook call with guardrails.

## 3) Recommended Python Stack (Windows)

Core libraries:
- Screen capture: dxcam (primary), mss (fallback)
- CV: opencv-python
- OCR: paddleocr (primary), pytesseract (fallback)
- UI automation: pywinauto + comtypes
- Input fallback: pyautogui

Optional quality add-ons:
- numpy for image transforms
- rapidfuzz for fuzzy matching OCR text to targets

Suggested install set:
- pip install dxcam mss opencv-python paddleocr pywinauto comtypes pyautogui rapidfuzz

## 4) LLMind Integration Points

Map new work to existing files:
- Hook schemas: hooks/hook_schemas.py
- Hook implementations: hooks/hook_registry.py
- Provider tool rendering: hooks/provider_adapters.py (already schema-driven)
- Request orchestration: network/requests.py
- App initialization and env defaults: main/LLMind.py

## 5) New Hooks to Add

Add these hooks first for clear separation of concerns.

1) capture_and_ocr_screen
Purpose:
- Capture full screen or region
- Run OCR
- Return normalized observations

Suggested args:
- action: capture
- region: {x, y, width, height} optional
- include_image: bool
- ocr_engine: paddle|tesseract|auto
- reason: string optional

Suggested return:
- image_path or image_base64 (bounded)
- ocr_blocks: [{text, confidence, bbox}]
- screen_size
- timestamp

2) analyze_ui_with_vision_model
Purpose:
- Send image + OCR summary + task objective to model
- Get strict JSON plan/candidate target

Suggested args:
- action: analyze
- objective: string
- image_ref
- ocr_blocks
- allowed_actions: [click, type, hotkey, scroll, noop]
- reason: string optional

Suggested return:
- decision: action type
- target: text/id/bbox
- confidence: float (0..1)
- rationale: short string
- fallback: optional alternate decision

3) verify_ui_change
Purpose:
- Validate expected state transition after an action

Suggested args:
- action: verify
- expected_text_any: [string]
- expected_text_all: [string]
- region optional
- timeout_ms optional

Suggested return:
- verified: bool
- observed_text
- confidence

## 6) Safety and Guardrails

Require all of the following before enabling autonomous actions:
- Env gate: LLMIND_ENABLE_UI_HOOKS=1
- New env gate: LLMIND_ENABLE_VISION_HOOKS=1
- Confidence threshold env:
  - LLMIND_VISION_MIN_CONFIDENCE (example default 0.72)
- Max retries env:
  - LLMIND_VISION_MAX_RETRIES (example default 2)
- Restricted action allowlist:
  - Click/type/hotkey only by default
  - Block destructive actions unless explicit confirmation mode is on

Confirmation policy examples:
- Always confirm: email send, file delete, process kill, admin prompts
- Auto mode allowed: read-only navigation and non-destructive clicks

## 7) Prompt Contract for Vision Reasoning

Use a strict response contract from the model. Example keys:
- decision
- target
- confidence
- fallback
- reason

Rules:
- Reject response if required fields are missing.
- Reject response if confidence < threshold.
- Reject response if decision not in allowlist.
- Never execute when parser fails.

## 8) Data Contract Example (JSON)

Perception output shape:
{
  "screen": {"width": 1920, "height": 1080},
  "ocr_blocks": [
    {"text": "Submit", "confidence": 0.94, "bbox": [812, 642, 902, 680]}
  ],
  "image_ref": "appdata://.../frame_2026-05-17T20-11-02.png"
}

Decision output shape:
{
  "decision": "click",
  "target": {"type": "text", "value": "Submit", "bbox": [812, 642, 902, 680]},
  "confidence": 0.88,
  "fallback": {"decision": "noop"},
  "reason": "Primary action button is visible and enabled"
}

## 9) Minimal Phased Rollout

Phase 1: Read-only perception
- Implement capture_and_ocr_screen only
- Save artifacts and logs
- No action execution

Phase 2: Assisted actions
- Add analyze_ui_with_vision_model
- Show proposed action to user for confirmation

Phase 3: Bounded autonomy
- Enable auto execution for allowlisted low-risk actions
- Add verify_ui_change and retry strategy

Phase 4: Full workflow integration
- Add hooks to orchestration allowlist
- Chain perception -> analyze -> act -> verify in workflow steps

## 10) Testing Strategy

Unit tests:
- OCR normalization
- Bounding-box filtering
- Confidence gating
- JSON response validation

Integration tests:
- Deterministic app targets (Notepad, Calculator)
- Golden screenshots for OCR regression checks
- Simulated model responses (good/bad/low confidence)

Failure tests:
- Empty OCR
- Mismatched target
- Popup interruptions
- DPI scaling differences

## 11) Performance Targets

Suggested initial SLOs:
- Screen capture: <= 120 ms
- OCR (region): <= 350 ms
- End-to-end perception step: <= 700 ms
- Action verify loop timeout: <= 2000 ms per attempt

## 12) Logging and Artifacts

Store per-step records in AppData artifacts:
- before.png
- after.png
- ocr.json
- decision.json
- action_result.json

Each record should include:
- run_id
- timestamp
- active window title/process if available
- selected strategy (UIA vs OCR vs CV)

## 13) Risks and Mitigations

Risk: OCR instability on themed/dynamic UIs
- Mitigation: region crop + contrast preprocess + fuzzy matching + retry

Risk: Wrong target click
- Mitigation: confidence threshold + post-click verify + safe fallback

Risk: Model hallucination in planning
- Mitigation: strict schema validation + allowlisted actions + confidence gate

Risk: Display scaling / multi-monitor issues
- Mitigation: normalize coordinates against current monitor bounds

## 14) Immediate Next Steps in This Repo

1. Add schema entries for:
- capture_and_ocr_screen
- analyze_ui_with_vision_model
- verify_ui_change

2. Add hook classes in hooks/hook_registry.py with:
- env gating
- strict arg checks
- bounded output sizes

3. Register new hooks in builtins and (optionally) orchestration allowlist.

4. Add a CLI self-test path in main/LLMind.py for a safe read-only vision smoke test.

5. Add one test target workflow:
- open app
- locate text/button
- propose action
- confirm
- verify

---

This guide is intentionally implementation-focused so you can adapt it as your provider mix (xAI/OpenAI/others) and desktop task scope evolve.
