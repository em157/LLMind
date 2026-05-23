# Provider Configurations for Tool Schemas

This project supports these four provider configurations for tool-calling workflows.

## xAi
- Provider key: xai
- Endpoint: https://api.x.ai/v1/chat/completions
- Auth header: Authorization: Bearer <API_KEY>
- Tool format: OpenAI Chat Completions function tools
- Suggested models: grok-4.3, grok-3

## ChatGpt (OpenAI)
- Provider key: openai
- Endpoint: https://api.openai.com/v1/chat/completions
- Auth header: Authorization: Bearer <API_KEY>
- Tool format: OpenAI Chat Completions function tools
- Suggested models: gpt-4.1-mini, gpt-4.1

## Anthropic
- Provider key: anthropic
- Endpoint: https://api.anthropic.com/v1/messages
- Auth headers:
  - x-api-key: <API_KEY>
  - anthropic-version: 2023-06-01
- Tool format: tools with input_schema
- Suggested models: claude-opus-4-5

## Gemini
- Provider key: gemini
- Endpoint pattern:
  - https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key=<API_KEY>
- Auth: API key in query parameter
- Tool format: tools[0].function_declarations
- Suggested models: gemini-2.0-flash

## Notes
- Keep tool usage minimal and explicit.
- Include a reason field in hook calls when available.
- Respect allowlists and guarded-command constraints.
