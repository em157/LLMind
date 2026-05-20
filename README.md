## Configuration & Secrets

LLMind reads configuration and credentials from environment variables. To set up local development:

1. Copy `.env.example` to `.env`.
2. Fill in real values (especially `OPENAI_API_KEY` and any SMTP variables if you use email hooks).
3. Keep `.env` local only — secrets must never be committed.

### Key environment variables

- `OPENAI_API_KEY`: API key for OpenAI requests.
- `LLMIND_BASE_DIR`, `LLMIND_SOURCE_FILE`, `LLMIND_DEST_DIR`: optional path overrides for local move-tool runner behavior.
- `LLMIND_ENABLE_UI_HOOKS`, `LLMIND_ENABLE_LAUNCH_HOOKS`, `LLMIND_ENABLE_COMMAND_HOOKS`, `LLMIND_ENABLE_WORKFLOW_HOOKS`, `LLMIND_ENABLE_EMAIL_HOOKS`: hook feature gates.
- `LLMIND_SMTP_*`: SMTP settings used by email hooks.

### Pre-commit checks

Install and run local guardrails before committing:

```bash
pip install pre-commit
pre-commit install
pre-commit run --all-files
```

The configured hooks include secret scanning (`detect-secrets`), absolute personal path detection, and notebook output stripping (`nbstripout`).
