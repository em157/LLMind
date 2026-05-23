# Prompt Workflow

Use this workflow to run prompt files, export a shareable bundle, and commit prompt updates safely on a branch.

## Quick start

From the repository root:

```powershell
.\main\PROMPT_WORKFLOW.cmd -Action run -Provider xai -PromptFile prompts/PROMPT_1_WINDOWS_UI_TRIAGE.md -KeyIndex 2
```

## Actions

### Run prompt file

```powershell
.\main\PROMPT_WORKFLOW.cmd -Action run -Provider openai -PromptFile prompts/PROMPT_2_BROWSER_VERIFY.md -Model gpt-4.1-mini -KeyIndex 1
```

Optional run flags:
- `-SystemInstructions "You are strict about tool safety"`
- `-Temperature 0.2`
- `-MaxTokens 600`

### Build share bundle

```powershell
.\main\PROMPT_WORKFLOW.cmd -Action share
```

This creates a zip file in `shared/` containing:
- Prompt markdown files in `prompts/`
- `scripts/prompt_workflow.ps1`
- `main/PROMPT_WORKFLOW.cmd`

### Commit workflow files on current branch

```powershell
.\main\PROMPT_WORKFLOW.cmd -Action commit -CommitMessage "Add provider prompt updates"
```

Commit behavior:
- Stages only prompt workflow assets (`prompts/*.md`, workflow scripts)
- Refuses to auto-commit on `main` or `master`
- Works best after switching to a feature branch

## Supported providers

- `xai`
- `openai` (ChatGpt)
- `anthropic`
- `gemini`

The script sets provider-specific endpoint defaults automatically.
