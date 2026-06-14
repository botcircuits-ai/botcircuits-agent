# 3. Providers

[← Index](00-index.md)

---

A **provider** is the adapter that talks to one LLM vendor. The agent loop only
knows the provider interface, so the same agent runs on any supported model.

## Supported

- **Anthropic** (Claude)
- **OpenAI** (GPT)
- **Gemini** (Google)

## What a provider hides

Every vendor has its own wire format for messages, tool calls, and tool results.
The provider translates the agent's neutral message shape into the vendor's
format and back — that conversion lives entirely inside the provider and never
leaks into the rest of the system.

## What a provider exposes

- **`complete()`** — one request/response round-trip.
- **`stream()`** — the same, but emitting events as they arrive.
- **capability flags** — small honest signals (e.g. "does this provider support
  hosted MCP?") so callers can adapt instead of pretending all vendors are
  identical.
- **token usage** — each call's real input/output/cache token counts are
  recorded, so cost is measurable.

## Choosing one

Set the provider and model in configuration (or per call). Switching from one
vendor to another is a config change, not a code change.

Next: [Tools, MCP & Skills](04-tools-mcp-skills.md).
