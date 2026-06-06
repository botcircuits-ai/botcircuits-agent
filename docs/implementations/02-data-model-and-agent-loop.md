# Data Model & The Agent Loop

[← Implementation Guide index](../../IMPLEMENTATION.md)

---

## 4. Normalized Data Model

Provider-neutral types live in [types.py](../../src/botcircuits/types.py):

| Type            | Purpose                                                                  |
|-----------------|--------------------------------------------------------------------------|
| `Message`       | One conversation turn; content is a list of typed blocks                 |
| `ToolCall`      | The model's request to invoke a tool: `id`, `name`, `arguments`          |
| `LLMResponse`   | One provider response normalized: `text`, `tool_calls`, `stop_reason`    |
| `StreamEvent`   | One event in a streamed agent turn (`text_delta`, `tool_call`, …)        |

The remaining shapes live next to whichever subsystem owns them:

| Type            | Module                                       |
|-----------------|----------------------------------------------|
| `LocalTool`     | [agent/tools/registry.py](../../src/botcircuits/agent/tools/registry.py) |
| `MCPServer`     | [agent/mcp.py](../../src/botcircuits/agent/mcp.py) |
| `SkillSpec`     | [agent/skill/spec.py](../../src/botcircuits/agent/skill/spec.py) |
| `LocalSkill`    | [agent/skill/local.py](../../src/botcircuits/agent/skill/local.py) |
| `MemorySnapshot`| [agent/memory.py](../../src/botcircuits/agent/memory.py) |

### Why blocks instead of strings?

A user message might be plain text, but an assistant turn can contain text **and** several tool calls; a follow-up user turn might be entirely tool results. Modeling this as a list of typed blocks (`text`, `tool_call`, `tool_result`) lets the same `Message` shape carry every kind of turn without conditional fields.

When a provider needs to send history back to its API, it walks the blocks and emits the right wire format:
- Anthropic: `tool_use` / `tool_result` content blocks
- OpenAI Responses: separate `function_call` and `function_call_output` items
- Gemini: `function_call` / `function_response` parts

The conversion is mechanical, lives entirely inside the provider, and never leaks.

---

## 5. The Agent Loop

[agent/core.py](../../src/botcircuits/agent/core.py). `Agent.chat()` and `Agent.chat_stream()` share the same logic; the streaming version yields events through it.

```python
for step in range(max_steps):
    response = await provider.complete(...)        # or .stream(...)

    record assistant turn (text + tool_calls)

    if response.stop_reason != "tool_use":
        return response.text

    run all requested tools concurrently
    record user turn (tool_results)
```

Three details that matter:

### 5.1 Concurrent tool execution
When the model returns multiple tool calls in one turn, they run via `asyncio.gather` (blocking) or `asyncio.as_completed` (streaming). Independent MCP queries fan out. Result blocks are appended to history in the **original order** to keep call/result pairing intuitive — even though they may have completed out of order.

### 5.2 The `max_steps` ceiling
Without a cap, a misbehaving model could loop forever. Default is 10 rounds per user turn; raise it for deeper agentic tasks. Configurable via JSON (`max_steps`) or `--max-steps`.

### 5.3 Per-conversation lock
`Conversation.lock` is an `asyncio.Lock`. Two concurrent `chat()` calls on the same `session_id` serialize automatically — without it, message order would corrupt. Different sessions still run in parallel.

### 5.4 Workflow advancement, human-feedback pause, and reminders

Two pieces of loop logic constrain workflow execution; both live in [agent/core.py](../../src/botcircuits/agent/core.py).

**Auto-recall (advancement).** When the model returns **no tool calls** but a workflow tool still holds a live `session_id`, the loop does *not* end the turn. Instead `_auto_recall_calls(reg)` synthesizes a tool call to each active workflow (empty args) and the loop runs it like any other tool call — the workflow tool's re-entry runs slot normalization and returns the next step's directive, which the model then acts on. This replaces the older design where the tool-result string and the system-prompt reminder *begged the model* to re-call the workflow tool itself; the model now only ever performs step actions, and the loop owns advancement. An empty-tool turn with **no** active workflow is still terminal (returns the text) — auto-recall is gated on `active_workflow_names(reg)` being non-empty and on `enable_workflows`.

**Human-feedback pause.** A `question`-type step (or the model's own judgment) routes a question to the user through the `human_feedback` builtin (§8.4). After the loop runs the tool calls for a turn, `_human_feedback_pause(tool_calls, results)` checks whether a `human_feedback` call ran; if so it returns that call's question and the loop ends the turn, surfacing the question as the reply. The user's next `chat()` call is their answer and resumes the run. This is a *terminal-turn* pause — it fits the existing `chat()` / REPL / gateway contract with no new control flow, and it deliberately suppresses auto-recall for that turn so the workflow doesn't advance past an unanswered question.

**System-prompt reminders.** Before every provider call, the loop runs the active system prompt through `_with_workflow_reminder()`. It appends one of two blocks:

- `[Active workflow]` — if any registered workflow tool reports `session_id != None` (mid-execution), telling the model to perform **only** the current step (call a tool, reply, or call `human_feedback` for a question) and **not** to call the workflow tool itself — the loop auto-recalls it. (Calling it manually would double-advance.)
- `[Available workflows]` — when *no* workflow is active but workflow tools exist, listing every registered workflow with its description and stating that calling the matching tool is **mandatory** as the model's first action whenever the user's request matches one. Without this, long histories cause the model to imitate prior "ask topic, then call tool" turns and skip the tool call entirely.

Computing this per-call (not caching on `convo.system`) costs one dict lookup but means the active set can change between turns without an invalidation step. See [§8.6 in Local Tools & Workflows](05-local-tools-and-workflows.md#86-botcircuits-workflows-as-tools) for the closure-state machinery that makes "mid-execution" detectable.

---
