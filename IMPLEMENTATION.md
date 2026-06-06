# Implementation Guide

This document explains the architecture, data flow, and *why* behind the design choices in `botcircuits-agent`. Read [README.md](README.md) first for usage; this file is for engineers extending or maintaining the system.

---

## 1. Goals & Non-Goals

### Goals
- One agent loop that works against any major LLM provider.
- First-class **MCP** support, with both hosted (provider-side) and local (in-process) modes.
- First-class **Skills** support — both *hosted* skills (mapping to each provider's code-execution surface) **and** *filesystem* skills (Claude-Code-style `SKILL.md` directories auto-discovered and exposed as tools).
- **Persistent memory** — MEMORY.md + USER.md under `~/.botcircuits/memories/` injected into the system prompt at session start; mutated via a `memory` tool with `add` / `replace` / `remove` actions.
- **Streaming** all the way down: a UI can watch text deltas, tool calls, and tool results as they happen.
- **Declarative configuration** via layered files in `~/.botcircuits/` and project `.botcircuits/`: `settings.json` for provider/model/tool config, `mcp.json` for MCP server entries, plus sibling `*.local.json` files for personal overrides.
- Pure Python, fully async, no framework lock-in (FastAPI is optional sugar).

### Non-Goals
- Persisting conversation history. The store is in-memory by design; persistence is a 30-line subclass if you need it.
- Hiding provider differences perfectly. We expose a few capability flags (`supports_hosted_mcp()`) so callers can adapt rather than pretend the world is uniform.
- Token-usage accounting, retries, or rate limiting. These belong in adapter layers and aren't part of the core loop.
- Loading arbitrary code from the JSON config. Tool *parameters* go in JSON; tool *implementations* live in code, where the security review can find them.

---

## 2. Package Layout

```
src/botcircuits/
├── __init__.py              # public API re-exports + .env loader
├── types.py                 # ToolCall, Message, LLMResponse, StreamEvent, ProviderStreamEvent
│
├── agent/                   # multi-round tool-use loop
│   ├── core.py              #   Agent (chat, chat_stream)
│   ├── store.py             #   ConversationStore + Conversation (memory snapshot injected here)
│   ├── memory.py            #   MEMORY.md / USER.md storage + read/mutate API + threat scrub
│   ├── mcp.py               #   MCPServer + LocalMCPManager
│   ├── skill/               #   skills — hosted spec + filesystem loader
│   │   ├── spec.py          #     SkillSpec (hosted code-exec request)
│   │   └── local.py         #     LocalSkill discovery, SKILL.md parser, render_body
│   └── tools/               #   local tools as a package, one file per tool
│       ├── registry.py      #     ToolRegistry + LocalTool
│       └── builtins/
│           ├── arithmetic.py       # `add`
│           ├── time.py             # `now`
│           ├── shell.py            # `shell_exec`  (y/N gated, foreground + background)
│           ├── shell_status.py     # `shell_status`
│           ├── shell_stop.py       # `shell_stop` (y/N gated)
│           ├── _bg.py              # background process registry + ring buffers
│           ├── read_file.py        # `read_file`
│           ├── write_file.py       # `write_file` (y/N gated)
│           ├── edit_file.py        # `edit_file`  (y/N gated, unified diff)
│           ├── list_dir.py         # `list_dir`
│           ├── glob_search.py      # `glob_search`
│           ├── grep_search.py      # `grep_search`
│           ├── todo_write.py       # `todo_write` (live list, in-memory store)
│           ├── plan_and_confirm.py # `plan_and_confirm` (y/N gated)
│           ├── build_workflow.py   # `build_workflow` (y/N gated, NL → workflow JSON + indexer; lazy)
│           ├── memory.py           # `memory` — add/replace/remove on MEMORY.md / USER.md
│           └── _confirm.py         # shared y/N + auto-mode helpers
│
├── agent/workflow/          # On-disk workflows registered as LocalTools
│   ├── __init__.py          #   fetch_workflows / run_workflow / workflow_tool /
│   │                        #   register_workflows / active_workflow_names
│   ├── local.py             #   discover *.json, drive engine, A-layer coercion
│   ├── condition_processor.py  # `workflow build` — NL conditions → choices + variables
│   ├── variable_normalizer.py  # B-layer LLM extraction on re-entry
│   └── engine/              #   trimmed port of botcircuits-runtime-handler STM
│       ├── executor.py      #     state-machine loop + pendingBranch resolver
│       ├── state.py         #     WorkflowStateContext (saved session, slots)
│       ├── utils.py         #     interpolation + next-state helpers
│       └── handlers/
│           ├── action.py    #       agentAction handler (action emit + branch setup)
│           └── choice.py    #       evaluate_choices helper, called on re-entry
│
├── providers/               # LLM backends, one file per provider
│   ├── base.py              #   LLMProvider ABC
│   ├── anthropic.py
│   ├── openai.py
│   └── gemini.py
│
├── cli/                     # interactive command-line client
│   ├── __main__.py          #   `python -m botcircuits.cli`
│   ├── app.py               #   arg parsing, REPL loop
│   ├── commands.py          #   slash-command dispatch (/help, /memory, /skills, /workflow add|edit, …)
│   ├── commands_mcp.py      #   `mcp add/remove/list/test` subcommands
│   ├── commands_workflow.py #   `workflow build --name=...` subcommand (writes to `.build/`)
│   ├── config.py            #   CLIConfig + JSON load/resolve/mutate (incl. workflow.normalize)
│   ├── settings.py          #   layered settings.json + mcp.json loaders + parsers
│   ├── system_prompt.py     #   DEFAULT_SYSTEM_PROMPT used when no user override is set
│   ├── render.py            #   stream / blocking renderers
│   └── ansi.py              #   color helpers
│
└── gateway/                 # FastAPI wrapper (JSON + SSE) + multi-channel message gateway
    ├── __main__.py          #   `python -m botcircuits.gateway`
    ├── app.py               #   FastAPI app + lifespan + provider/registry + MessageGateway
    ├── routes.py            #   /healthz, /chat, /chat/stream, /sessions/{id}/reset, /messaging/status
    ├── schemas.py           #   pydantic request/response
    ├── sse.py               #   Server-Sent Events serializer
    ├── messaging.py         #   MessageGateway — channel registry + inbound→agent→outbound routing
    ├── messaging_config.py  #   env + .botcircuits/messaging.json loader
    └── channels/            #   pluggable platform adapters (one file per channel)
        ├── base.py          #     Channel ABC, InboundMessage, OutboundMessage, ChannelError
        ├── whatsapp.py      #     Meta WhatsApp Cloud API (verify GET + events POST + Graph send)
        ├── slack.py         #     Slack Socket Mode (outbound WebSocket via slack_sdk, chat.postMessage)
        ├── webhook.py       #     Generic webhook (Bearer in, configurable POST out)
        └── cron.py          #     60s-tick scheduler, 5-field UTC cron matcher, optional fan-out
```

**Why this shape.** Each file is one responsibility. Providers are siblings of `agent/` because they're a swappable backend the agent depends on through an ABC, not internals of the loop. The `tools/builtins/` package gives every new tool one file plus one entry in a dispatch table. The CLI is split so config parsing, slash commands, and the chat REPL can each be tested without dragging in the others.

---

## 3. High-Level Architecture

```
   ┌────────────────────┐  ┌────────────────────┐  ┌────────────────────┐
   │   CLI (REPL)       │  │  FastAPI Gateway   │  │   Library use      │
   │  botcircuits-cli   │  │  /chat /chat/stream│  │  Agent(...)        │
   └─────────┬──────────┘  └─────────┬──────────┘  └─────────┬──────────┘
             │                       │                       │
             ▼                       ▼                       ▼
   ┌────────────────────────────────────────────────────────────────────┐
   │                              AGENT                                 │
   │                  (agent/core.py — async loop)                      │
   │                                                                    │
   │  chat() / chat_stream(user_input, session_id) → StreamEvent...     │
   │                                                                    │
   │  ┌─────────────────────────┐    ┌────────────────────────────────┐ │
   │  │  ConversationStore      │    │ _with_workflow_reminder()      │ │
   │  │  per-session lock       │    │ append "[Active workflow]" to  │ │
   │  │  + Message history      │    │ system prompt if any workflow  │ │
   │  └─────────────────────────┘    │ tool holds a live session_id   │ │
   │                                 └────────────────────────────────┘ │
   │                                                                    │
   │  for step in range(max_steps):                                     │
   │      response = await provider.complete/stream(...)                │
   │      record assistant turn (text + tool_calls)                     │
   │      if stop_reason != "tool_use": break                           │
   │      run tool_calls concurrently → tool_result blocks              │
   └───┬────────────────────────────────────────────────────────────┬───┘
       │ tools=registry.all()                                       │ hosted_mcp + skills
       ▼                                                            │ (passed through)
   ┌──────────────────────────────────────────────────┐             │
   │              TOOL REGISTRY                       │             │
   │              (one flat namespace)                │             │
   │                                                  │             │
   │ ┌──────────────────┐  ┌─────────────────────┐    │             │
   │ │ Built-in tools   │  │ Local-MCP tools     │    │             │
   │ │ (default_registry│  │ (LocalMCPManager    │    │             │
   │ │  in agent/tools/ │  │  wraps each MCP     │    │             │
   │ │  builtins/)      │  │  tool as a          │    │             │
   │ │                  │  │  LocalTool named    │    │             │
   │ │ add  now         │  │  "<srv>__<tool>")   │    │             │
   │ │ read/write/edit  │  └──────────┬──────────┘    │             │
   │ │   _file          │             │               │             │
   │ │ list_dir         │             ▼ stdio / http  │             │
   │ │ glob/grep_search │      ┌─────────────────┐    │             │
   │ │ shell_exec       │      │ Local MCP       │    │             │
   │ │ shell_status     │      │ servers         │    │             │
   │ │ shell_stop       │      │ (in-process     │    │             │
   │ │ todo_write       │      │  ClientSession) │    │             │
   │ │ plan_and_confirm │      └─────────────────┘    │             │
   │ │   (y/N gated via │                             │             │
   │ │    _confirm.py)  │  ┌─────────────────────┐    │             │
   │ └──────────────────┘  │ Workflow tools      │    │             │
   │                       │ (one LocalTool per  │    │             │
   │                       │  BotCircuits        │    │             │
   │                       │  workflow record;   │    │             │
   │                       │  see GUARDRAIL box) │    │             │
   │                       └──────────┬──────────┘    │             │
   └─────────────────────────────────││───────────────┘             │
                                     ││                             │
              ┌──────────────────────┘└────────────────────┐        │
              │                                            │        │
              ▼                                            ▼        ▼
   ┌──────────────────────────────────┐    ┌─────────────────────────────────┐
   │       PROVIDER (LLMProvider)     │    │     HOSTED CAPABILITIES         │
   │       provider.complete()        │    │     (provider executes them)    │
   │       provider.stream()          │    │                                 │
   │                                  │    │  hosted_mcp = MCP servers the   │
   │  ┌───────────┐ ┌──────────────┐  │    │   provider runs server-side     │
   │  │ Anthropic │ │ OpenAI       │  │    │                                 │
   │  │ Messages  │ │ Responses    │  │    │  skills =                       │
   │  │  API      │ │  API         │  │    │   Anthropic Skills bundles      │
   │  └───────────┘ └──────────────┘  │    │   OpenAI code_interpreter       │
   │  ┌───────────────────────────┐   │    │   Gemini code_execution         │
   │  │ Gemini generate_content   │   │    │                                 │
   │  └───────────────────────────┘   │    │  (Gemini lacks hosted MCP →     │
   │                                  │    │   auto-promoted to local)       │
   │  Normalizes wire format ↔        │    └─────────────────────────────────┘
   │   Message blocks (text /         │
   │   tool_call / tool_result)       │
   └──────────────────────────────────┘
                  │
                  ▼ HTTPS
        ┌─────────────────────┐
        │  LLM cloud APIs     │
        │  (Anthropic /       │
        │   OpenAI / Gemini)  │
        └─────────────────────┘


   ╔═══════════════════════════════════════════════════════════════════╗
   ║       WORKFLOW TOOL  =  GUARDRAIL  (agent/workflow/)              ║
   ║                                                                   ║
   ║  At startup: register_workflows(registry, provider=..., ...)      ║
   ║    1. Glob .botcircuits/workflows/*.json                          ║
   ║       (or $BOTCIRCUITS_WORKFLOWS_DIR)                             ║
   ║    2. Wrap each record as a LocalTool with closure state          ║
   ║         state = {"session_id": None}                              ║
   ║       handler signature widened: (args, context=None)             ║
   ║    3. Built-in tool names always win on collision (skipped, with  ║
   ║       a yellow warning).                                          ║
   ║                                                                   ║
   ║  Author-time: `botcircuits-cli workflow build --name=<wf>` (or   ║
   ║    the `build_workflow` tool via `/workflow add|edit`).           ║
   ║    Compiles NL `conditions` on agentAction states into:           ║
   ║      • per-condition `expCondition` annotation                    ║
   ║      • `choices[]` (operator + expressionList + next)             ║
   ║      • `flow.variables[]` (variableName, dataType, description)   ║
   ║    Uses the SAME LLM provider/model the agent is configured with. ║
   ║    Writes the built result to `<dir>/.build/<name>.json`; the     ║
   ║    raw source under `<dir>/<name>.json` is the author's editable  ║
   ║    file. Runtime loads only from `.build/`.                       ║
   ║                                                                   ║
   ║  At call time (local.run_workflow → engine.run_flow):             ║
   ║    a. Load the workflow file by id                                ║
   ║    b. Resume from saved session (currentStep, pendingBranch,     ║
   ║       slots) if any                                               ║
   ║    c. RE-ENTRY only, when pendingBranch is set: normalize args    ║
   ║         • Layer B — provider.complete(...) extracts values using  ║
   ║           the variable schema + last_assistant_message; drops     ║
   ║           hallucinations via string-presence check                ║
   ║         • Layer A — coerce to dataType; drop on failure           ║
   ║    d. Merge normalized args into slots                            ║
   ║    e. Executor: if pendingBranch was set, evaluate choices to     ║
   ║       pick next state; otherwise walk from currentStep           ║
   ║    f. On an agentAction step, return immediately:                 ║
   ║         {action, done, conditions, choices, variables, ...}       ║
   ║       If that action has choices, record pendingBranch on the     ║
   ║       saved session so the NEXT re-entry triggers normalization.  ║
   ║    g. Persist the paused session in _SESSIONS[session_id] so the  ║
   ║       next call resumes from currentStep.                        ║
   ║                                                                   ║
   ║  Supported step types only (state.type at top level):             ║
   ║    • start        → no-op                                         ║
   ║    • agentAction  → emit action payload, pause workflow           ║
   ║       (branches via `conditions`/`choices` evaluated on RE-ENTRY) ║
   ║    `choice` as a state type is NOT supported — branching lives    ║
   ║    inside agentAction.                                            ║
   ║                                                                   ║
   ║  Multi-turn — the guardrail:                                      ║
   ║    • Each workflow run emits ONE agentAction per call.            ║
   ║    • Tool result tells the model "call this tool again to advance"║
   ║    • While state.session_id is set, agent loop appends            ║
   ║      "[Active workflow] you MUST call '<name>' again..."          ║
   ║      to the system prompt on every provider call.                 ║
   ║    • Workflow ends → state.session_id = None → reminder stops.    ║
   ║                                                                   ║
   ║  Cost: branching states pay ONE extra LLM call (Layer B) on       ║
   ║  re-entry. Non-branching agentActions and initial calls pay zero. ║
   ║  No LLM/RAG fallback inside the engine, no interruption handling  ║
   ║  — pure in-process beyond the optional normalization round-trip.  ║
   ╚═══════════════════════════════════════════════════════════════════╝
```

Three layers, top to bottom:
1. **Agent** — owns the loop, history, system-prompt augmentation (workflow reminder + persistent memory snapshot), and tool dispatch.
2. **Provider** — translates normalized requests into provider-specific API calls.
3. **MCP / Skills / Workflows / Memory** — capability extensions:
   - **Built-in tools** live in-process and are gated per-call via `_confirm.py`.
   - **Local MCP** servers are run by us and exposed as `LocalTool`s the provider never sees as "MCP."
   - **Hosted MCP / Hosted Skills** are passed through to the provider as native parameters.
   - **Filesystem skills** (§8b) are `SKILL.md` directories auto-discovered from `./skills/` and `./.botcircuits/skills/` and wrapped as `LocalTool`s; their rendered bodies (with live `` !`cmd` `` substitutions) ride the same dispatch path as any other tool.
   - **Workflow tools** are `LocalTool`s wrapping on-disk BotCircuits workflows, driven by the embedded STM engine in [agent/workflow/engine/](src/botcircuits/agent/workflow/engine/); their multi-turn state machine + system-prompt reminder forms the **guardrail layer** that constrains the LLM to a workflow-defined path. At runtime only the indexed copies under `.build/` are loaded; raw sources are author-time files.
   - **Persistent memory** (§8a) is a flat-file store under `~/.botcircuits/memories/` rendered into the system prompt at session start; the `memory` tool is how the agent writes back.

The Agent never calls a provider's SDK directly; the Provider never sees Anthropic-specific block types leak across the boundary. This isolation is what makes provider swaps trivial.

---

## 4. Normalized Data Model

Provider-neutral types live in [types.py](src/botcircuits/types.py):

| Type            | Purpose                                                                  |
|-----------------|--------------------------------------------------------------------------|
| `Message`       | One conversation turn; content is a list of typed blocks                 |
| `ToolCall`      | The model's request to invoke a tool: `id`, `name`, `arguments`          |
| `LLMResponse`   | One provider response normalized: `text`, `tool_calls`, `stop_reason`    |
| `StreamEvent`   | One event in a streamed agent turn (`text_delta`, `tool_call`, …)        |

The remaining shapes live next to whichever subsystem owns them:

| Type            | Module                                       |
|-----------------|----------------------------------------------|
| `LocalTool`     | [agent/tools/registry.py](src/botcircuits/agent/tools/registry.py) |
| `MCPServer`     | [agent/mcp.py](src/botcircuits/agent/mcp.py) |
| `SkillSpec`     | [agent/skill/spec.py](src/botcircuits/agent/skill/spec.py) |
| `LocalSkill`    | [agent/skill/local.py](src/botcircuits/agent/skill/local.py) |
| `MemorySnapshot`| [agent/memory.py](src/botcircuits/agent/memory.py) |

### Why blocks instead of strings?

A user message might be plain text, but an assistant turn can contain text **and** several tool calls; a follow-up user turn might be entirely tool results. Modeling this as a list of typed blocks (`text`, `tool_call`, `tool_result`) lets the same `Message` shape carry every kind of turn without conditional fields.

When a provider needs to send history back to its API, it walks the blocks and emits the right wire format:
- Anthropic: `tool_use` / `tool_result` content blocks
- OpenAI Responses: separate `function_call` and `function_call_output` items
- Gemini: `function_call` / `function_response` parts

The conversion is mechanical, lives entirely inside the provider, and never leaks.

---

## 5. The Agent Loop

[agent/core.py](src/botcircuits/agent/core.py). `Agent.chat()` and `Agent.chat_stream()` share the same logic; the streaming version yields events through it.

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

### 5.4 Workflow re-entry / availability reminder
Before every provider call (both `chat` and `chat_stream`), the loop runs the active system prompt through `_with_workflow_reminder()` ([agent/core.py](src/botcircuits/agent/core.py)). It appends one of two blocks:

- `[Active workflow]` — if any registered workflow tool reports `session_id != None` (mid-execution), instructing the model to re-call that tool to advance.
- `[Available workflows]` — when *no* workflow is active but workflow tools exist, listing every registered workflow with its description and stating that calling the matching tool is **mandatory** as the model's first action whenever the user's request matches one. Without this, long histories cause the model to imitate prior "ask topic, then call tool" turns and skip the tool call entirely.

Computing this per-call (not caching on `convo.system`) costs one dict lookup but means the active set can change between turns without an invalidation step. See [§8.6](#86-botcircuits-workflows-as-tools) for the closure-state machinery that makes "mid-execution" detectable.

---

## 6. Provider Abstraction

```python
class LLMProvider(ABC):
    async def complete(self, system, messages, tools, hosted_mcp,
                       skills, max_tokens) -> LLMResponse: ...
    async def stream(self, system, messages, tools, hosted_mcp,
                     skills, max_tokens):  # async generator
        yield ...
    def supports_hosted_mcp(self) -> bool: return False
    async def aclose(self) -> None: ...
```

A provider receives:
- `tools` — every callable available to the model (built-in local tools + user-registered + local-MCP-derived). The provider exposes these as the model's "function tools."
- `hosted_mcp` — MCP servers the **provider** will execute itself (relevant only when `supports_hosted_mcp()` is true).
- `skills` — hosted code-execution intents.

The provider is responsible for:
1. Translating `messages` into its wire format.
2. Wiring `hosted_mcp` and `skills` into vendor-specific parameters.
3. Calling its SDK.
4. Normalizing the response into `LLMResponse`.
5. (For streaming) yielding `("text_delta", str)` chunks plus a final `("final", LLMResponse)`.

### 6.1 Anthropic ([providers/anthropic.py](src/botcircuits/providers/anthropic.py))
- Uses `client.beta.messages` whenever any beta header is needed (MCP, Skills).
- Hosted MCP via `mcp_servers` parameter + `mcp-client-2025-11-20` beta.
- Skills via `container.skills` + `code_execution_20250825` tool + three beta headers.
- Streaming uses `client.messages.stream(...)`, which yields a `text` event for each delta and lets us call `get_final_message()` for the assembled result.

### 6.2 OpenAI ([providers/openai.py](src/botcircuits/providers/openai.py))
- Uses the **Responses API** (`client.responses.create`), not Chat Completions. Reason: hosted MCP and `code_interpreter` only exist on Responses.
- Hosted MCP entries go into the `tools` array as `{"type": "mcp", "server_label", "server_url", "require_approval"}`.
- Skills become `{"type": "code_interpreter", "container": {"type": "auto"}}`. `skill_id` is Anthropic-specific and ignored here.
- Streaming uses `stream=True` and listens for `response.output_text.delta` for live text and `response.completed` to grab the assembled response (which contains the final `function_call` items).

### 6.3 Gemini ([providers/gemini.py](src/botcircuits/providers/gemini.py))
- Uses `client.aio.models.generate_content` for native async.
- **No hosted MCP** as of 2026; configs with `mode="hosted"` are warned and skipped, **or** auto-promoted to `local` by the Agent.
- Skills map to `Tool(code_execution=ToolCodeExecution())`.
- Local function tools are wrapped as Python callables with synthesized signatures so genai's introspection picks up parameter names from `input_schema`.
- Streaming uses `client.aio.models.generate_content_stream(...)`; each chunk's `chunk.text` is the new text piece. Final `function_call` parts are read from accumulated chunks.
- Automatic function calling is **disabled** so all tool dispatch flows through the same outer agent loop, keeping history consistent across providers.

---

## 7. MCP: Hosted vs Local

[agent/mcp.py](src/botcircuits/agent/mcp.py). The single `MCPServer` config has a `mode` field:

```python
@dataclass
class MCPServer:
    name: str
    mode: Literal["hosted", "local"] = "hosted"
    url: str | None = None
    transport: Literal["http", "sse", "stdio"] = "http"
    command: str | None = None      # local stdio
    args: list[str] = []
    authorization_token: str | None = None
    allowed_tools: list[str] | None = None
    require_approval: Literal["always", "never"] = "never"
```

### 7.1 Hosted mode
The Agent collects all `mode="hosted"` servers and passes them through to `provider.complete(..., hosted_mcp=...)`. The provider wires them into its own MCP parameter. The provider's runtime does the entire round trip — list tools, call tools, return results — server-side.

### 7.2 Local mode — the `LocalMCPManager`
This is the part that makes MCP usable on every provider, including ones without hosted support.

```python
class LocalMCPManager:
    async def start(self):
        # 1. Open each MCP session inside an AsyncExitStack
        # 2. Call list_tools() on each
        # 3. Wrap each MCP tool as a LocalTool with a namespaced name
        #    "{server_name}__{tool_name}"
```

Why this works: from the model's perspective, an MCP tool and a Python function tool are identical — both have a name, a description, and a JSON schema. By exposing MCP tools **as** `LocalTool` instances, every provider handles them through the same code path it already uses for built-in and user tools. No provider needs to know MCP exists for local mode.

The handler returned by `_make_handler` is a closure that:
1. Takes the model's argument dict.
2. Awaits `session.call_tool(name, args)`.
3. Flattens the MCP result's content parts to text.
4. Raises if the MCP server reports `isError`, which the registry turns into an `is_error: True` tool result.

A single `asyncio.Lock` serializes calls into the manager because the MCP `ClientSession` isn't documented as concurrent-safe. Drop the lock if your servers handle concurrency.

### 7.3 Auto-promotion
When `provider.supports_hosted_mcp()` is `False` and a config has `mode="hosted"`, the Agent flips it to `"local"` at construction time and prints an info line. This means a single config works across providers — Gemini just runs everything locally.

### 7.4 Declaring servers in JSON
MCP servers live in `.botcircuits/mcp.json`, separate from `settings.json`. Each file's on-disk shape is `{"servers": {"<name>": {<fields without name>}}}` — the dict key is the server name. The layered loader ([cli/settings.py](src/botcircuits/cli/settings.py)) reads three tiers (`~/.botcircuits/mcp.json`, `.botcircuits/mcp.json`, `.botcircuits/mcp.local.json`), merges by name with later tiers winning, and injects the merged list into the resolved settings dict as `mcp_servers`. Putting an `mcp_servers` block in `settings.json` is rejected at parse time with a pointer to `mcp.json`. The CLI's `mcp add/remove/list/test` subcommands ([cli/commands_mcp.py](src/botcircuits/cli/commands_mcp.py)) operate on one layer at a time (default: project shared; `--user` / `--local` pick others) — writing back to a merged view would silently inline user-level entries into the project file. Server entries written by the CLI strip default-valued fields so the file stays minimal.

The merged list **replaces** any servers passed to `Agent(mcp_servers=...)` in code. The CLI and gateway both read it and pass the resolved list straight through.

---

## 8. Local Tools — Package Layout & Per-Tool Config

[agent/tools/](src/botcircuits/agent/tools/). Each built-in tool lives in its own file under `builtins/` and exposes two things:

```python
def shell_exec_tool(*, auto=False, ...) -> LocalTool:
    """Factory — captures policy in a closure so the model can't override it."""

def register(reg: ToolRegistry, **config) -> None:
    """Threaded by `default_registry()` from `tools.<name>` in JSON.
    Validates keys, then calls the factory."""
```

`default_registry(tools_config)` walks a single `_BUILTINS` dispatch table and calls each `register(reg, **overrides)`. Per-tool config in JSON looks like:

```json
{
  "tools": {
    "shell_exec": { "timeout_seconds": 60, "auto": false },
    "now": null
  }
}
```

Three behaviors:
- **dict** → register the tool with those overrides on top of the factory's defaults.
- **`null`** or **`false`** → skip registration entirely (disable the tool).
- **omitted** → register with the factory's defaults.

Validation happens at startup in [agent/tools/__init__.py](src/botcircuits/agent/tools/__init__.py) (unknown tool names → reject) and in each tool's own `register()` (unknown override keys → reject). The CLI exits 2 with a clear message before any provider is built.

### 8.1 Why this shape
The earlier "kitchen-sink" `tools.py` couldn't accommodate more than three or four tools without becoming unreadable. One file per tool: trivial to add, trivial to delete, trivial to ship a tool optionally without dragging it into the default surface. The `register(reg, **config)` contract gives every tool a uniform override surface so the JSON schema doesn't grow when you add a tool — it grows by one key.

### 8.2 The `shell_exec` tool
[agent/tools/builtins/shell.py](src/botcircuits/agent/tools/builtins/shell.py). Runs a system command via `asyncio.create_subprocess_exec` — **no shell**, so pipes, redirects, globs, and metacharacters are literal. Default config:

| Param | Default |
|---|---|
| `timeout_seconds` | 30 |
| `max_output_bytes` | 10 KB |
| `auto` | `False` |

There is intentionally **no command allow-list**. The gate is a human y/N confirmation prompt that runs before every command. The model proposes argv; the tool prints it on stderr, reads a y/N response from stdin, and either runs the command or returns `{"denied": true, ...}` with a hint to the model not to retry the same argv.

**Auto mode.** `auto=True` skips the prompt. A warning banner still prints on stderr before each command so the user sees what ran. Set via `--auto` on the CLI or `tools.shell_exec.auto: true` in JSON. The CLI flag is fanned out by `cli/app.py:_AUTO_GATED_TOOLS` into the `auto` key of every gated tool (`shell_exec`, `shell_stop`, `write_file`, `edit_file`, `plan_and_confirm`, `build_workflow`), merged *after* `resolve()` so it overrides the JSON value when both are set. `--auto` on a tool disabled with `"<tool>": null` is intentionally a no-op — disabling and automating are contradictory states; we don't resurrect a tool the user explicitly turned off.

**Effective-auto for non-tty.** The constructor computes `effective_auto = auto or not sys.stdin.isatty()`. The FastAPI gateway and piped CLI invocations have no human to answer the prompt, so they automatically engage auto mode regardless of config — otherwise every tool call would deadlock waiting for stdin that never arrives. This is evaluated once at tool construction; tools live for the life of the process, so a one-shot check is correct.

**Confirmation prompt UX.** The prompt writes to **stderr**, not stdout, so it doesn't get mixed into the streamed assistant text on stdout. Color is auto-disabled when stderr isn't a TTY or `NO_COLOR` is set. The reader uses `loop.run_in_executor(None, input, "")` so it doesn't block the event loop — provider streaming continues, MCP heartbeats keep firing, the agent loop stays responsive.

**Why no allow-list.** A read-only allow-list conflates "what's safe by default" with "what's useful by default" — the obvious read-only set blocks `git`, `python`, `pip`, anything that mutates, so any real agentic task needs to override it. The model then hits rejections, retries with a different argv, and burns tokens. A y/N gate is more honest: the human decides per-call, the model sees real denials with explicit reasoning, and there's no policy buried in a constant somewhere. For unattended runs, `auto=True` is one keystroke.

**Why no cwd pinning.** Pinning a working directory looks like sandboxing but isn't — absolute paths in argv bypass it entirely (`['cat', '/etc/passwd']` doesn't care where the process started). The user is already gating every call via y/N, so the cwd guardrail adds friction without adding safety. The subprocess inherits the agent process's cwd; the user controls where the agent is launched.

The tool description string (sent to the model) **announces the confirmation gate and the limits** so the model adapts its behavior — it knows to expect a denied result and what argv it can use.

The tool is in `default_registry()` because the confirmation gate makes the default surface safe by construction.

### 8.3 Adding a new built-in tool
Three steps:
1. Create `agent/tools/builtins/<name>.py` with a factory and a `register(reg, **config)`.
2. Add one entry to `_BUILTINS` in [agent/tools/__init__.py](src/botcircuits/agent/tools/__init__.py).
3. (Optional) Document the config keys in the README.

That's it. Users can configure or disable it via `tools.<name>` in JSON without any further plumbing.

If the new tool needs y/N gating, import `from . import _confirm` and call `_confirm.effective_auto(auto)` once at construction, then `_confirm.confirm(title, lines)` / `_confirm.warn(title, lines)` per call. Register `auto` as one of the tool's allowed config keys, then add the tool name to `_AUTO_GATED_TOOLS` in [cli/app.py](src/botcircuits/cli/app.py) so `--auto` covers it.

**Lazy-registered builtins.** Some tools are heavy enough that we don't want them on the model's tool list for normal chat — `build_workflow`, today — so [agent/tools/__init__.py](src/botcircuits/agent/tools/__init__.py) maintains a `_LAZY_BUILTINS = ("build_workflow",)` set. `default_registry()` skips lazy builtins unless the user explicitly opted in via `tools.<name>` in JSON. The CLI's slash dispatcher loads them on demand via `register_builtin(reg, name, *, provider, config)`, which is a no-op if the tool is already on the registry. The wiring lives in `LAZY_TOOL_TRIGGERS` in [cli/commands.py](src/botcircuits/cli/commands.py) — one entry maps `/workflow` to the `build_workflow` tool name and the slash handler calls `register_builtin(...)` before forwarding the rest of the line as a chat message. Adding a new lazy trigger is one line in each map.

**Provider-aware builtins.** Tools that need the agent's `LLMProvider` at register time (e.g. `build_workflow` runs the workflow indexer against the same model the agent chats with) are listed in `_PROVIDER_AWARE_TOOLS`. `default_registry(tools_config, *, provider=None)` injects `provider=` into those tools' `register()` kwargs automatically; CLI and gateway both pass the constructed provider through. Library callers who omit `provider` get a working tool with whatever provider-dependent step is skipped — `build_workflow` writes the raw source but doesn't index.

### 8.4 The code-gen tool surface

`shell_exec` alone is enough to drive a build, but it forces the model to invent argv for things the runtime can do natively (read a file, replace a substring, walk a tree for matches). The other code-gen builtins are dedicated tools for those operations so the model gets structured input/output and the user gets a uniform gating UX.

| Tool | Why dedicated (vs. shell_exec) |
|---|---|
| `read_file` | Returns `{start_line, end_line, total_lines, truncated, content}` — the model knows what it has without parsing `wc -l` output. `offset`/`limit` for large files. |
| `write_file` | Y/N prompt shows path + byte count + content preview before the write. Parent dirs auto-created. |
| `edit_file` | Y/N prompt shows a **unified diff**, not the new content alone. Enforces unique-match contract for `old_string` (or `replace_all=true`). Mirrors Claude Code's Edit semantics. |
| `list_dir` | Returns typed entries (`file`/`dir`/`symlink`) with sizes — easier than parsing `ls -la` output. |
| `glob_search` | Pure-Python `glob.glob(recursive=True)`. Sorts by mtime (newest first), caps results, and skips common ignore dirs (.git, node_modules, __pycache__, .venv, …). |
| `grep_search` | Pure-Python regex walk. Skips binary files and the same ignore dirs. Optional filename `include` glob. Bounded by `max_results` + `max_file_bytes`. |
| `todo_write` | Replace-semantics list (model passes the whole list each call). Module-global `_STORE` so `plan_and_confirm` can seed it. Renders to stderr with colored glyphs. |
| `plan_and_confirm` | Y/N prompt shows the plan + initial TODO list before any work starts. Seeds `_STORE` on approval. The default system prompt instructs the model to call this once per non-trivial software task. |
| `build_workflow` | Y/N prompt shows the workflow summary + ordered step preview (branches inline as `↳ if <NL> → <next>`). Validates against the supported step types, writes the raw source under `.botcircuits/workflows/`, then runs the condition indexer and emits a runnable copy to `.botcircuits/workflows/.build/`. Lazy-registered (loaded on `/workflow add|edit` only). See §8.6.10. |
| `memory` | Mutates persistent agent + user notes that live across sessions. Three actions: `add` (append), `replace` (substring-match swap), `remove` (substring-match drop). Two targets: `memory` (agent's notes; 2200-char cap) and `user` (user profile; 1375-char cap). Content is auto-loaded into the system prompt at session start — no `read` action because the data is already in context. See §8a. |

**The planning gate is a normal LocalTool, not a hardcoded loop step.** Putting it in the tool registry has three benefits: (1) the gate is opt-in via the JSON config (disable with `"plan_and_confirm": null` if you don't want it), (2) the model chooses *when* to invoke it based on the task — pure-question requests skip it, (3) `--auto` and the y/N prompt reuse the same `_confirm` helper as every other gated tool, so the UX stays consistent.

**Why no command allow-list / path sandbox for these tools.** Same logic as `shell_exec` (§8.2): an allow-list conflates "safe by default" with "useful by default." A `write_file` restricted to `./src/**` blocks the model from creating tests, configs, or sibling-directory artifacts. The y/N gate puts the human in the loop per call; `--auto` is the explicit escape hatch for trusted/unattended runs. Path traversal isn't a meaningful concern when the user sees the path in the prompt before approving.

**Why module-global `_STORE` in `todo_write`.** The store lives in the tool module, not on the Agent, because it's intrinsically tied to the *tool's* lifetime within a single process. Putting it on `Agent` would couple two unrelated concerns and force every consumer (gateway included) to thread a reference. The cost: two processes can't share a TODO list — fine, because each `botcircuits-cli` run is its own session.

**Default system prompt.** [cli/system_prompt.py](src/botcircuits/cli/system_prompt.py) holds `DEFAULT_SYSTEM_PROMPT`. The CLI applies it inside `load_cli_config` when neither `--system` nor JSON `system` is set. The gateway stores it on `app.state.default_system` and the routes fall back to it when `req.system` is absent. The prompt teaches the model the planning workflow: ask focused follow-up questions when ambiguous, call `plan_and_confirm` for non-trivial software tasks, keep `todo_write` fresh, verify with tests, and use `background: true` for non-terminating commands. It also documents the **persistent memory** contract (see §8a) — when MEMORY.md / USER.md are loaded they appear under `<agent_memory>` / `<user_profile>` tags, the `memory` tool is how the agent updates them, and edits take effect on the NEXT session (the snapshot is frozen at session start to keep the prompt cache warm). Users override the prompt entirely with `--system "..."` (empty string disables the default).

### 8.5 Background shell processes

`shell_exec(background: true)` exists because foreground-only execution is hostile to anything that doesn't terminate: dev servers, file watchers, `tail -f`, `uvicorn --reload`, `npm run dev`. With a finite `timeout_seconds`, those commands all hit the timeout and get killed — the model sees a useless error.

The model surface is three tools: `shell_exec` (with `background: true`), `shell_status`, `shell_stop`. The state lives in [agent/tools/builtins/_bg.py](src/botcircuits/agent/tools/builtins/_bg.py) as a module-global `_REGISTRY` dict keyed by short uuids. Same module-global pattern as `todo_write._STORE`: the registry is intrinsically tied to its tools' lifetime in one process, and putting it on `Agent` would force every consumer to thread a reference.

**Tail buffers as bounded `collections.deque`.** Each background process spawns two reader tasks (`_pump`) that read lines from the pipe into `deque(maxlen=MAX_LINES_PER_STREAM)`. Bounded so a runaway producer can't OOM the agent; per-line truncated at `LINE_BYTES` so a process that never emits newlines can't either. `shell_status` returns `tail(buf, lines)` slices — the model gets the most recent context without ever seeing the full history.

**Why deques, not strings.** Concatenating strings is O(n²) for a chatty producer; deque appends are O(1). The model wants the *recent* tail anyway, not the start.

**Why pipe-readers as background tasks, not on-demand reads.** If we lazily read in `shell_status`, the pipe buffer (~64KB on macOS) fills and the producer blocks. The model would see a "frozen" process that's actually waiting for someone to drain its stdout. Pumping continuously keeps the producer unblocked even if the model never polls.

**Cleanup has two layers.** `Agent.aclose()` awaits `_bg.terminate_all()` — graceful SIGTERM-then-SIGKILL while the event loop is still alive. As a backstop, the first call to `_bg.register` installs an `atexit` hook that falls back to synchronous `os.kill` (the loop is gone by then, so we can't `await` cleanly). This catches the case where someone forgets `async with Agent(...)` and the process exits anyway. Orphaned `npm run dev` after a CLI exits is unfriendly enough to justify the two-layer approach.

**Why `shell_stop` is gated but `shell_status` isn't.** `shell_status` is pure observation — never modifies state, can't fail in a way that needs human approval. `shell_stop` kills a process; that's a real side effect of the same magnitude as starting one, so it goes through `_confirm.confirm` with the same `auto` semantics. `shell_stop` is also in `_AUTO_GATED_TOOLS`, so `--auto` covers it.

**`terminated_exit_code`, not `exit_code`.** The registry's is-error heuristic (§5.x) flags any tool result with non-zero `exit_code` as an error. But a SIGTERM-killed process has `returncode = -15`, and that's the *successful* outcome of `shell_stop` — not a failure observation. Returning the key under a different name keeps the heuristic accurate while preserving the information.

### 8.6 BotCircuits workflows as tools

[agent/workflow/](src/botcircuits/agent/workflow/). Workflows are loaded from a local directory and each one is exposed as a `LocalTool` on the same `ToolRegistry` that holds the built-ins. From the model's perspective a workflow looks identical to any other tool — same name/description/schema surface — so no provider needs to know workflows exist.

Public functions in [agent/workflow/__init__.py](src/botcircuits/agent/workflow/__init__.py):

| Function | Purpose |
|---|---|
| `fetch_workflows()` | Return the workflow records discovered on disk. |
| `run_workflow(workflow_name, args, *, session_id, provider, last_assistant_message, last_user_message, normalize_enabled)` | Execute one step of a workflow and return its result. Threads `session_id` through so subsequent calls re-enter the same workflow conversation. `provider` + the message snapshots feed Layer B normalization (see §8.6.4). |
| `workflow_tool(record, *, provider, normalize_enabled)` | Wrap a single workflow record as a `LocalTool` with closure state (`{"session_id": None}`) for multi-turn execution. The handler accepts an optional `context` dict from the agent loop. |
| `register_workflows(reg, *, provider, normalize_enabled)` | Discover workflows + wrap each as a `LocalTool` and register on `reg`. Returns `(registered_names, skipped_names)`. |
| `active_workflow_names(reg)` | List the names of workflow tools on `reg` that currently hold a live `session_id` (i.e. are mid-execution). Read by the agent loop to inject a re-entry reminder. |

#### 8.6.1 Discovery + loader (raw source vs. `.build/` artifact)

[agent/workflow/local.py](src/botcircuits/agent/workflow/local.py). Workflows live in two parallel locations under `$BOTCIRCUITS_WORKFLOWS_DIR` (default `.botcircuits/workflows`):

- **Raw source** — `<workflows-dir>/<name>.json`. The human-editable file the author maintains. Carries `name`, `description`, and natural-language `conditions` on `agentAction` states. `conditions` lives at the step **root** (sibling of `type`/`next`), not nested inside `settings` — it describes control flow, not step-type-specific payload.
- **Build artifact** — `<workflows-dir>/.build/<name>.json`. The built, runnable copy with `expCondition` strings, `choices[]` arrays (also at the step root), and an aggregated `flow.variables` list. Written by the `workflow build` CLI command and the `build_workflow` tool.

`fetch_workflows()` reads only from `.build/`. A missing `.build/` directory or `.build/<name>.json` for an existing raw file is reported with a stderr warning telling the user to run `botcircuits-cli workflow build --name=<name>`; the workflow is then skipped rather than loaded un-built. Rationale: an un-built workflow has natural-language conditions the engine can't evaluate, so silently loading it would surface as cryptic "no choice matched" failures at runtime. Skipping with an actionable error message keeps the failure mode obvious.

`name` is the sole identifier — it doubles as the registered LLM-facing tool name, so it must match `^[a-zA-Z0-9_-]+$` (OpenAI's strictest tool-name regex). The loader validates this and defaults to the filename stem when the field is missing.

`_load_workflow_record(workflow_name)` re-reads the build artifact on every `run_workflow` call so on-disk edits pick up without a restart (the file in `.build/` is the source of truth at runtime; we never cache it across calls).

#### 8.6.2 The embedded STM engine

[agent/workflow/engine/](src/botcircuits/agent/workflow/engine/) is deliberately narrow. The whole engine is four small modules:

| File | Role |
|---|---|
| `executor.py` | `run_flow(flow, message, start_step_id, journey_id)` walks step-by-step until a step yields data (the `agentAction` pause) or the graph runs out of steps. On re-entry it also resolves `pendingBranch` (see below). |
| `state.py` | `WorkflowStateContext` tracks `currentStep`, `runningStep`, `pendingBranch`, and per-journey `slots` across re-entries. |
| `handlers/choice.py` | `evaluate_choices(choices, message, default_next)` — pure helper for expression evaluation. No longer a state type; called by the executor when resolving a `pendingBranch`. Operators: `is`/`is not`, `>`/`>=`/`<`/`<=`, `contains`/`not contains`, `starts with`/`ends with`, `is empty`/`is not empty`. Typed values from the indexer (`500` not `"500"`) compare correctly because the helper coerces string `value` against typed `variable` at comparison time. |
| `handlers/action.py` | Builds the payload `{action, conditions, choices, variables, end, nextStep, slots, ...}` the workflow tool surfaces to the LLM. The executor routes by `step.type == "agentAction"`, so this handler doesn't re-check the kind. |
| `utils.py` | `fill_text_with_slots` (case-insensitive `{slot}` interpolation), `get_next_step_for_prompt_action`, `coerce_for_compare`. |

**Step kind: a single `state.type` field, two values.** Since only one action sub-kind is supported, kind is set by one top-level field rather than threaded through multiple discriminators:

| `state.type` | What it does |
|---|---|
| `start` | No-op; the executor falls through to `next`. |
| `agentAction` | Calls `handle_action`; emits the action payload and pauses the workflow. If `step.conditions` or `step.choices` is present (both at the step root, sibling of `type`/`next`), the executor records `pendingBranch` on the saved session so the **next** re-entry resolves the branch instead of walking blindly to the static `next`. |

Anything else (`choice`, `message`, `prompt`, `aiTask`, `webhook`, `codehook`, `integrationWorkflow`, `journey`, `human_support`, `pause`, `custom_action`, `end_action`, `auth_action`) raises `ValueError(f"... does not support state type {state_type!r} ...")` with a hint pointing authors to the agentAction-with-conditions pattern. Silent no-ops were considered and rejected: they make broken workflows look like working ones.

**Why no separate `choice` state type.** Branching evaluates the *current* state of slot values; for an LLM-driven workflow those values exist only *after* the model has acted on the previous agentAction (the model captures them and re-calls the workflow tool with args). Putting branching inside `agentAction` makes that timing explicit — the executor knows it needs to wait for the LLM to fill variables before evaluating. A standalone `choice` state would either fire too early (against stale slots) or require synchronous LLM intervention to fill slots first, which is exactly what we don't want — branching stays deterministic.

**The `pendingBranch` mechanism.** When an `agentAction` with conditions pauses, the executor sets `saved_session["pendingBranch"] = {"stepId": <id>, "defaultNext": <static next>}`. On the next call, before walking from `currentStep`, `_resolve_pending_branch` reads the step's `choices`, calls `evaluate_choices(choices, message, defaultNext)`, and uses the result as the entry point — overriding `currentStep`. The marker is then cleared so it doesn't fire again. This keeps the engine purely sequential between turns (no embedded async branching), while still routing on the latest slot values.

When no `choice` matches, the engine falls through to `defaultNext` (or ends the workflow if there isn't one). There is no LLM/RAG fallback inside the engine — recovery, if any, is the agent loop's job, not the workflow's.

#### 8.6.3 Building — `workflow build --name=<wf>`

[agent/workflow/condition_processor.py](src/botcircuits/agent/workflow/condition_processor.py). The author writes natural-language `conditions` on an `agentAction`. The builder compiles them into a typed `choices` array and writes `flow.variables` once. Run via the CLI:

```bash
uv run botcircuits-cli workflow build --name=order_status
```

What it does:

1. Walks every `agentAction` state with a non-empty `conditions` array (dedupes per state).
2. Builds a prompt: each condition's NL text + the surrounding state graph (so the LLM picks sensible variable names by looking at neighboring action text).
3. Calls `provider.complete(...)` with a strict-JSON system prompt — **same `LLMProvider` the agent runs on**, no tools, no streaming.
4. Parses each returned expression (`<variable> <operator> <value>`) back into the `{variable, operator, value}` shape the engine reads, and aggregates `variables[]` across all states.
5. Writes the built result to `<workflows-dir>/.build/<name>.json`, leaving the raw source under `<workflows-dir>/<name>.json` untouched. Idempotent: re-running replaces (not appends to) the generated `choices` and the top-level `variables` list. The raw source is the canonical input; if the user edits the raw file and forgets to re-build, `fetch_workflows()` prints a warning and skips that workflow until the next build run.

**Operator allow-list mirrors the engine.** The prompt enumerates the same 12 operators `evaluate_choices` understands; the LLM is told to use *only* those. Anything else is dropped at parse time.

**Why the indexer lives next to the engine.** The engine is the consumer; the indexer's output must match the engine's input contract exactly (same operator names, same `expressionList` shape). Keeping them together means a change to one is one PR.

**Why same provider as the agent.** The user already picked an `LLMProvider` for chat; reusing it for indexing means one model behavior, one set of credentials, one place to swap providers. The alternative — a hardcoded model just for indexing — would mean a separate dependency, a separate API key, and a separate place where output quality could drift.

#### 8.6.4 Variable normalization on re-entry — A + B

When the LLM re-calls a workflow tool after an `agentAction` with conditions, the values it passes (`order_total="500"`, `order_status="has been delivered"`) rarely match the choice expressions exactly. [agent/workflow/local.py](src/botcircuits/agent/workflow/local.py) runs a two-layer normalization pipeline before merging args into slots and handing control to the executor.

**Gate.** Both layers run *only* when **all three** are true: `saved_session.pendingBranch` is set (the prior turn paused on a branching agentAction), the state has any variables to coerce, and (for Layer B) a `provider` was wired in. Initial calls and re-entries into non-branching actions skip the whole pipeline — they pay zero extra LLM calls.

**Layer B — `variable_normalizer.normalize(...)`.** [agent/workflow/variable_normalizer.py](src/botcircuits/agent/workflow/variable_normalizer.py). One `provider.complete(...)` round-trip with `tools=[]`, `hosted_mcp=[]`, `skills=[]`. Inputs:

- The **filtered** variable schema: `variables_for_step(flow, pending_step_id)` walks the step's `choices[].expressionList[].variable` and returns only the matching entries from `flow.variables`. Listing irrelevant variables wastes tokens and tempts hallucination.
- The raw tool args, the action text, and `last_assistant_message` (provided by the agent loop via the tool's `context`).

The model returns `{normalized: {variableName: value, ...}}`. Three post-processing steps:

1. **Allow-list restriction.** Drop any key the model invented that isn't in the filtered schema.
2. **Hallucination guard.** Each value must appear (case-insensitive substring) somewhere in the source context (args JSON + action text + last assistant message). Booleans always pass (trivially present in any text); numbers match both as-typed (`500`) and stripped (`500.0` → `500`); empty strings pass (so `is empty` checks fire correctly). Values that don't appear get dropped with a stderr warning.
3. **Failure tolerance.** Any provider error, malformed JSON, or schema mismatch returns `{}` and logs a single stderr line. The workflow never aborts because of B; it degrades to "raw args + Layer A."

**Layer A — `_coerce_variables(...)`.** [agent/workflow/local.py](src/botcircuits/agent/workflow/local.py). Deterministic type coercion against `flow.variables[].dataType`. Always runs when the workflow has an indexed schema, regardless of whether B ran. Behavior:

| dataType | Accepts | Produces | Drops |
|---|---|---|---|
| `number` | `int`, `float`, `"500"`, `"500.5"`, `"1e3"` | `int` or `float` | `bool`, words, currency-decorated strings |
| `boolean` | `True`/`False`, `"true"`/`"false"`/`"yes"`/`"no"`/`"on"`/`"off"`/`"1"`/`"0"`/`""` | `True`/`False` | ambiguous strings (`"maybe"`) |
| `string` | anything | `str(value).strip()` | nothing (string is the catch-all) |

Coercion failures emit `[workflow] dropping <name>=<value>: cannot coerce to <type>` and **omit the key from the merged slots**. The default branch in the choice evaluator is the safety net — a missing slot reads as the empty string, so `is empty` fires correctly and other operators no-match.

**Why drop on failure rather than raise.** The workflow's job is to keep the conversation moving. A coercion failure means the LLM didn't give us a usable value; the right answer is to take the default branch (often `apologize` / `ask_clarification` / similar) rather than crash the whole user-facing flow. The stderr warning makes the bug visible to the operator without breaking the run.

**Why no string case-folding.** Tempting (`"Delivered"` → `"delivered"` matches the enum), but it would corrupt case-sensitive identifiers like `"SKU-ABC"` or `"PROD-001"`. The indexer's prompt instead pushes enum values toward lowercase, and Layer B's `description` hint (e.g. `"one of: pending | shipped | delivered"`) lets the model normalize semantically. If a workflow really needs case-insensitive enum matching, an optional `allowedValues` field on the variable schema would be a future addition.

**Settings knob.** `workflow.normalize: true|false` in `settings.json` toggles Layer B. Validated by `cli/config.py::_parse_workflow`; unknown keys inside the `workflow` block are rejected at startup. Layer A always runs.

**The normalized values reach the engine via the same slot path the runtime already uses** — `run_workflow` writes them into `session_context.slots`, the executor hands the `message` to `evaluate_choices`, and the choice handler reads `message["data"]["sessionContext"]["slots"][variable]`. The engine itself doesn't know normalization happened; B and A are pure I/O fixups around the existing slot pipeline.

#### 8.6.5 Session resumption

A workflow pauses on every `agentAction`. The engine returns the action and the saved session (`{currentStep, slots, runningStep, pendingBranch, ...}`); `local.run_workflow` keeps that saved session in a module-global `_SESSIONS: dict[str, dict]` keyed by `session_id`. On re-entry with the same `session_id`:

1. Look up the saved session; if `pendingBranch` is set, normalize args (Layer B + Layer A) and run `_resolve_pending_branch` to override the entry point.
2. Merge (normalized) incoming `args` into per-journey slots (slot values stick across calls).
3. Run the engine from `currentStep` (or the branched-to state if step 1 fired) until the next `agentAction` pause or end.

When the workflow ends (or returns no action), `_SESSIONS.pop(session_id)` drops the entry so a future call with that id starts fresh. `_SESSIONS` is module-global for the same reason as `todo_write._STORE` and `_bg._REGISTRY` — the lifetime is tied to the tool's process, and routing it through `Agent` would force every consumer to thread a reference.

The tool result on a non-terminal step is `{"status": "ok", "workflow_name", "session_id", "action", "done": false, "messages": [<engine frame>], "conditions", "choices", "variables"}`. On a terminal turn (or one with no action), `action` is `None` and `done` is `True`.

#### 8.6.6 Multi-turn execution — closure state per workflow tool, context-aware handler

`workflow_tool()` captures a tiny mutable dict in the tool's handler closure so a single workflow can span many LLM turns. The handler signature is widened to accept an optional `context` dict from the agent loop (see §8.6.7):

```python
state: dict[str, str | None] = {"session_id": None}

async def _handler(args: dict, context: dict | None = None) -> str:
    ctx = context or {}
    result = await run_workflow(
        wf_id, args,
        name=wf_name,
        session_id=state["session_id"],
        provider=provider,                                       # B's provider
        last_assistant_message=ctx.get("last_assistant_message", ""),
        normalize_enabled=normalize_enabled,
    )
    if (not result.get("action")) or result.get("done"):
        state["session_id"] = None
        return f"Workflow '{wf_name}' finished."
    state["session_id"] = result.get("session_id")
    return f"{action}\n\n(call '{wf_name}' again to advance)"
```

The first call passes `session_id=None`; `run_workflow` mints a uuid and `_SESSIONS` keys the workflow conversation on it. Subsequent calls re-enter the same `session_id` so the engine advances the same workflow instance instead of starting a new one. The state clears when the workflow ends so the *next* invocation starts fresh.

`tool._workflow_state` is exposed (assigned post-construction) so the agent loop can introspect mid-run workflows without touching the closure directly — see `active_workflow_names()` above.

#### 8.6.7 Handler context plumbing — `LocalTool.handler(args, context=None)`

Layer B needs the last assistant message to ground its hallucination guard. Pulling it out of the agent's `Conversation` requires the workflow tool to see something it doesn't naturally have — surrounding loop state. The fix is a single optional second argument on every tool handler.

**Registry-side introspection.** [agent/tools/registry.py](src/botcircuits/agent/tools/registry.py) inspects each handler's signature once at call time. A handler accepts `context` if it has ≥2 positional parameters, or a parameter named `context`, or `**kwargs`. Two-arg handlers receive `(args, context)`; one-arg handlers receive `(args)` unchanged. This is *additive* — every existing builtin keeps working without modification.

**Loop-side population.** [agent/core.py](src/botcircuits/agent/core.py) builds a `tool_context` dict once per turn before dispatching tool calls, and passes the same snapshot to every concurrent tool call in that turn:

```python
tool_context = {
    "last_assistant_message": _last_assistant_text(convo.messages),
    "last_user_message":     _last_user_text(convo.messages),
    "session_id":            convo.session_id,
}
results = await asyncio.gather(*[
    self.tools.run(tc.name, tc.arguments, tool_context)
    for tc in resp.tool_calls
])
```

`_last_assistant_text` / `_last_user_text` walk `convo.messages` in reverse and return the most recent text block of that role (truncated to 2KB via `_CONTEXT_LAST_ASSISTANT_CHARS`), or `""` if none exists yet. `_last_user_text` deliberately skips `tool_result` blocks (which also live on user-role messages) — Layer B wants the human's actual utterance, not tool output that already landed.

**Why a snapshot, not a live reference.** Tools run concurrently via `asyncio.gather` / `asyncio.as_completed`. Passing a snapshot avoids races where one tool mutates state another tool is reading. The 2KB cap is the only token-budget knob the loop applies to context; Layer B's prompt does the rest of the trimming.

**Why a dict, not a typed object.** Future fields (`recent_tool_results`, `recent_user_message`, etc.) are likely. A dict means adding one is a one-liner in `core.py` and an opt-in read in the tool that wants it — no signature change, no version bump.

#### 8.6.8 System-prompt re-entry reminder

The inline hint in the tool result ("call '<name>' again to advance") is not always sticky — across a long tool-use turn the model can forget, drift, or treat the action as a one-shot. [agent/core.py:_with_workflow_reminder](src/botcircuits/agent/core.py) handles this by appending a `[Active workflow]` block to the system prompt **for every provider call** while any workflow tool reports `session_id != None`:

```
[Active workflow] The workflow tool '<name>' is mid-execution. After you
finish the action of the current step, you MUST call '<name>' again
(with empty args) to receive the next step. Skip the re-call only when
the current step asks the user a question and you need their reply first.
```

The reminder is computed per-call (not stored on `convo.system`) because the active set can change between turns — a workflow that just finished should stop nagging the model on the next turn. Computing it inline costs one dict lookup per provider call; cheap, and the alternative (caching) would have to be invalidated on every workflow state change.

At most one workflow runs at a time today (the loop picks `names[0]`); the data structure supports a list because a future "parallel workflows" feature is plausible.

#### 8.6.9 Design choices

**Branching in `agentAction`, not a separate `choice` type.** Branching evaluates the slot values *after* the LLM has had a chance to fill them. A standalone `choice` state would fire too early — slot values from the previous agentAction don't exist until the LLM re-calls the workflow tool with args. Folding branching into `agentAction` makes the dependency on LLM action explicit: emit → LLM acts → re-enter → branch. The `pendingBranch` marker is what bridges the gap between "we paused with conditions" and "now we have the values to evaluate them against."

**Built-ins take precedence on name collision.** `register_workflows` walks the records and skips any whose name is already registered. The CLI prints a yellow `[workflow] skipped (name collides with built-in tool): ...` line so the conflict is visible. Rationale: a workflow accidentally named `shell_exec` must never silently shadow the built-in tool — security-relevant tools need stable identities.

**Why a separate subpackage, not another builtin.** A workflow isn't a single tool; it's *N* dynamically-discovered tools whose surface depends on the workflows directory. Putting it in `agent/tools/builtins/` would force one factory per workflow at import time, which doesn't work — discovery happens async at startup and the set can change between runs. The subpackage owns the load / register lifecycle and produces standard `LocalTool` instances the registry already knows how to handle.

**Why no LLM/RAG fallback on unmatched choices.** The engine is meant for *deterministic* agent-action graphs. When no choice matches, the LLM that's already driving the agent loop is the natural fallback — it sees the workflow ended and decides what to do next. Embedding an extra LLM hop inside the engine would just duplicate that.

**Why no interruption handling.** There is no end-user inside the engine — the LLM is the caller, and it controls when to re-enter the workflow via the system-prompt reminder. Interruption is just "the LLM chose to call a different tool"; no plumbing needed.

**A + B, not A or B.** Type coercion alone (A) doesn't fix semantic drift (`"shipped already"` → `"shipped"`). LLM normalization alone (B) is non-deterministic and can produce values that fail the runtime's typed comparisons (`"500"` vs `500`). Stacking them means B handles meaning and A guarantees the type contract before the engine sees the slots. If B fails (provider error, bad JSON), the workflow still has A as a guaranteed minimum, plus the executor's default-branch fallthrough — never aborts.

**CLI wiring.** [cli/app.py](src/botcircuits/cli/app.py) calls `register_workflows(registry, provider=provider, normalize_enabled=cfg.workflow["normalize"])` after `default_registry()` runs (so built-ins are present and collisions are detectable). `LocalWorkflowError` (raised when a workflow file is malformed) is caught and reported as `[workflow] ...` with exit code 2 — same pattern as the tools-config error path. The `workflow build` subcommand reuses the same provider construction via `load_cli_config(args)` + `make_provider(...)`, so author-time inference and runtime normalization always run on the same model.

#### 8.6.10 Authoring workflows — the `build_workflow` built-in

[agent/tools/builtins/build_workflow.py](src/botcircuits/agent/tools/builtins/build_workflow.py). The agent can create or update workflow JSON files mid-chat without the user hand-editing anything. The tool collapses four otherwise-manual steps into one call:

1. **Validate** the model-supplied `workflow` payload against the supported step set (`start`, `agentAction`), confirm every `next`/`conditions[].next` pointer resolves to a known step id, require `settings.action` on each `agentAction`, and assert the `name` is slug-safe (`^[a-zA-Z0-9_-]+$` — OpenAI's strictest tool-name regex, since the name doubles as the LLM-facing tool name). Validation errors return `{error: "..."}` so the model sees the failure rather than writing a half-broken file.
2. **Confirm.** Render a y/N block with the required `summary` string and an ordered step preview computed by walking from `start` along `next`. Branches show inline (`↳ if '<condition>' → <next>`). The block uses the shared [`_confirm`](src/botcircuits/agent/tools/builtins/_confirm.py) helper so the UX matches `plan_and_confirm` / `write_file` / `edit_file`. Denying returns `{denied: true, message: "..."}` with explicit anti-retry guidance.
3. **Write the raw source.** Writes the un-indexed workflow under `.botcircuits/workflows/<name>.json` as `{name, description, flow: {start, steps}}` — the source-of-truth file the human can re-open and hand-edit later. The write happens *before* indexing so the editable copy lands on disk even if the LLM-driven step below fails.
4. **Index + emit the build artifact.** Deep-copies the raw record, runs `condition_processor.generate_expressions_and_variables(flow, provider)` on the *same* `LLMProvider` the chat is using, and writes the indexed copy to `.botcircuits/workflows/.build/<name>.json`. **The agent runtime only loads from `.build/`** — see §8.6.1 — so an un-built workflow isn't callable. If indexing raises, the build artifact is intentionally **not** written (so a stale un-built copy never masquerades as runnable); the result carries `index_error` pointing the user at `botcircuits-cli workflow build --name=<name>` to retry manually. If no provider is wired in at all, the result carries `index_note` with the same recovery instruction.

**The `on_built` callback — live tool registration without a restart.** `build_workflow_tool(...)` takes an optional `on_built` callback fired after a runnable build artifact lands on disk. The CLI installs one (see `_make_workflow_refresh_callback` in [cli/commands.py](src/botcircuits/cli/commands.py)) that re-runs `register_workflows(agent.tools, provider=...)` so the agent picks up the new/edited workflow as a callable tool on the very next turn. The callback only fires when `built_written` is true — failed indexing produces no runnable artifact and therefore no live registration. Both sync and async callbacks are supported; the tool awaits the return value when it's awaitable.

**Provider plumbing.** Most builtins are pure stdlib; `build_workflow` is the first one that needs the agent's `LLMProvider` at register time. The plumbing is one new optional kwarg on `default_registry(tools_config, *, provider=None)` plus a small `_PROVIDER_AWARE_TOOLS = ("build_workflow",)` allow-list — when present, the provider is threaded into that tool's `register(reg, **config)` call. The CLI passes `provider=provider` from [cli/app.py](src/botcircuits/cli/app.py); the gateway does the same in [gateway/app.py](src/botcircuits/gateway/app.py). Library callers who omit the kwarg get a working `build_workflow` that writes the file but skips indexing — the same fallback path as an indexer failure.

**Lazy registration.** `build_workflow` is in `_LAZY_BUILTINS`, so `default_registry()` *skips* it by default — only the explicit `/workflow add|edit` slash command (or a `tools.build_workflow: {}` entry in JSON) loads it. Rationale: 99% of chat turns don't author workflows, but the tool's description is large enough to be worth keeping off the model's tool catalog when not in use. The slash handler calls `register_builtin(agent.tools, "build_workflow", provider=..., config={"on_built": _refresh})` once, which is a no-op on subsequent `/workflow` invocations within the same session.

**Why not the `agent/workflow/` subpackage.** `register_workflows()` lives there because it discovers per-record tools at startup. `build_workflow` is the inverse — one fixed tool that *produces* records. It belongs alongside the other gated stdlib-only builtins, not in the workflow loader. The two share `condition_processor` but nothing else, so the import is one targeted line, not a structural coupling.

**Why lazy imports inside the file.** `agent/tools/__init__.py` is reachable from `providers/base.py` via `agent.mcp`, so top-level `from ....providers.base import LLMProvider` would re-trigger the same circular-import the rest of the package already dodges. `LLMProvider` is `TYPE_CHECKING`-only here; `condition_processor` is imported *inside* the handler so the heavyweight provider/types graph stays out of module-load time. The cost is one extra `import` lookup per call — negligible next to a multi-second LLM round-trip.

**Why the input schema mirrors the on-disk file.** The model already knows the workflow file shape (it's documented in the workflow section above and frequently appears in example JSON). Reusing `steps`/`settings` keys plus a step-root `conditions` means the LLM doesn't have to learn a parallel intermediate schema for the tool — what it would write to disk by hand is what it passes as `workflow.steps` here. The `name` field doubles as the filename and as the registered tool name so the model picks a single identifier rather than two.

**Why `conditions` lives at the step root, not inside `settings`.** `settings` holds the *step-type-specific payload* — for `agentAction` that's `settings.action`, the natural-language instruction the LLM has to execute. `conditions` (and its compiled sibling `choices`) describes *where to go next*, which is control flow — the same category as `type` and `next`. Putting all control-flow fields at the step root keeps the mental model clean (`type`/`next`/`conditions` cluster together) and lets a future step type carry an entirely different `settings` schema without touching the branching surface.

**Why guidance lives in the tool description.** The tool's `description` carries the full authoring flow (clarify first, call once, report path / index_error, don't hand-edit with write_file). The system prompt stays generic code-gen behavior — per-tool rules belong with the tool so they appear in the model's tool catalog and vanish when the tool is disabled, matching the convention already used by `plan_and_confirm` / `write_file` / `edit_file`.

---

## 8a. Persistent Memory (MEMORY.md / USER.md)

[agent/memory.py](src/botcircuits/agent/memory.py) + [agent/tools/builtins/memory.py](src/botcircuits/agent/tools/builtins/memory.py). Modeled after Hermes Agent's persistent memory feature. Two flat markdown files under `~/.botcircuits/memories/` (overridable via `$BOTCIRCUITS_MEMORY_DIR`):

- `MEMORY.md` — agent's notes about the environment, project conventions, and lessons learned. Cap: **2200 chars** (~800 tokens).
- `USER.md` — user profile: preferences, communication style, role, expectations. Cap: **1375 chars** (~500 tokens).

### 8a.1 Read path — frozen snapshot at session start

`ConversationStore.get_or_create(...)` ([agent/store.py](src/botcircuits/agent/store.py)) calls `render_for_system_prompt(load_snapshot())` exactly **once**, at session creation, and appends the result to the system prompt for that conversation. The snapshot is then frozen — mutations made via the `memory` tool mid-session are written to disk but do *not* feed back into the live conversation. Rationale: re-reading on every turn would invalidate the Anthropic prompt cache (snapshot delta = new prefix), and within a single session the model can already see what it just wrote in tool-result blocks. Users get the updated snapshot on the next session.

The rendered block is wrapped in `<user_profile>` and `<agent_memory>` tags so the model can tell where persistent memory ends and the rest of the prompt begins. When both files are empty, `render_for_system_prompt` returns `""` so a first-run user doesn't see weird trailing whitespace.

### 8a.2 Write path — the `memory` tool

The `memory` LocalTool exposes three actions, all targeted at one of `{"memory", "user"}`:

| Action | Args | What it does |
|---|---|---|
| `add` | `target`, `text` | Append a new entry. Idempotent: identical text already present returns `{added: false, reason: "entry already present"}`. |
| `replace` | `target`, `old_text`, `new_text` | Substring-match an existing entry and swap text. Errors when zero or >1 entries match — the model is expected to disambiguate with a longer substring. |
| `remove` | `target`, `old_text` | Substring-match and drop an entry. Same uniqueness rule as `replace`. |

There is intentionally **no** `read` action — content is already in the system prompt. Documenting `read` would just invite the model to burn a tool call on something it already has.

### 8a.3 Storage format

Entries are separated by `§` (section sign) delimiters on their own line, so multi-line entries are first-class. `_split_entries` drops empty leading/trailing entries so blank slots don't accumulate across round-trips. `_join_entries` adds a trailing newline so `cat`-style inspection stays tidy and diffs are clean.

### 8a.4 Capacity enforcement

Every `add`/`replace` runs `_check_cap` against the target's character cap and raises `MemoryError` when the new content would exceed it — the tool returns the error as a normal tool-result so the model can react ("consolidate or remove an entry before adding new content"). At ≥80% capacity the success response includes a soft `"hint": "Consider consolidating soon..."` so the model has runway to cleanup before hitting the hard limit. Caps are characters, not tokens, because we render directly into the prompt as text — the token budget is the user's, not the API's.

### 8a.5 Threat scrub

`_scan_for_threats` rejects:

- **Prompt-injection patterns** — case-insensitive matches against a small allow-list (`ignore previous instructions`, `disregard the system prompt`, `</system>`, `<|im_start|>`/`<|im_end|>`, exfiltration phrasing like "send me the API key"). Conservative — false positives are cheap (the model retries with rephrased text); false negatives mean weaponized text lands in the prompt.
- **Invisible Unicode** — any Cf (format-control) or Cc (other-control) character except `\n` / `\t` / `\r`. Zero-width joiners and bidi-overrides have been used to hide payloads from human reviewers; we'd rather refuse than store them.

The scrub runs on both `add` and `replace.new_text`. `remove` doesn't need it (no new content being written).

### 8a.6 Slash command — `/memory`

`/memory` in [cli/commands.py](src/botcircuits/cli/commands.py) prints the on-disk directory plus a per-target summary: entry count, total used/cap chars, and a numbered preview of each entry's first line (truncated to 160 chars). Read-only — mutations go through the tool so the model and human share one code path.

### 8a.7 Why not just put memory in JSON config

JSON config is parameters (provider, model, tool flags) — declarative, versioned alongside the project. Memory is *content* — accumulated across sessions, often personal, and the model writes to it. Mixing the two would mean either JSON the model can't safely edit, or memory the user has to maintain by hand. The split keeps each surface honest.

---

## 8b. Filesystem Skills (Claude-Code-style)

[agent/skill/local.py](src/botcircuits/agent/skill/local.py). Distinct from the hosted **`SkillSpec`** below (§9): a *filesystem skill* is a directory containing a `SKILL.md` file with YAML-ish frontmatter and a markdown body. We discover skills from a list of root directories, parse each `SKILL.md`, and expose each one as a `LocalTool` the model can call. When the model invokes the tool, the handler re-renders the body — including any `` !`cmd` `` shell substitutions — and returns the rendered string so the model follows fresh instructions every time.

### 8b.1 Discovery roots

`DEFAULT_SKILL_ROOTS = ("skills", ".botcircuits/skills")`. Earlier roots win on name collisions. The `Agent` constructor takes a `local_skills_paths` kwarg to override the default; passing `[]` disables filesystem skills entirely. Discovery runs inside `Agent.start()`, *after* user tools and MCP tools are merged into the registry, so a skill named `shell_exec` can never shadow the built-in shell tool.

### 8b.2 SKILL.md format

```markdown
---
name: my-skill                 # slug, defaults to directory name
description: what this does    # model-facing trigger
allowed-tools: shell_exec, edit_file   # space- or comma-separated hint list
disable-model-invocation: false        # "true" keeps it out of model's tool list
---

Body markdown the model receives when the skill is invoked.
Current branch: !`git branch --show-current`

```!
git diff --stat
```
```

Frontmatter unknown keys are ignored so future SKILL.md versions with richer metadata still load. Names must match `^[a-z0-9][a-z0-9-]{0,63}$` (Claude-Code's rule). If `description` is absent, the first paragraph of the body is used.

### 8b.3 Dynamic substitutions — `` !`cmd` `` and ```` ```! ```` blocks

`render_body()` runs two substitutions in order:

1. **Fenced ` ```! ` blocks** — the block body is one multi-line shell command; the output replaces the block as ` ```text ... ``` `. Done first so an inline `!` inside a fenced command isn't double-expanded.
2. **Inline `` !`cmd` `` placeholders** — backtick-wrapped command preceded by `!`, only when `!` starts a line or follows whitespace (`KEY=!`cmd`` is intentionally not substituted; matches Claude Code's rule).

Commands run via the shell (so pipes work), in the **skill's directory** (not the agent's cwd), with a per-command **10s timeout**. Failures are non-fatal: a timed-out or erroring command becomes a `[error: ...]` marker inline and the rest of the body still renders. Rationale — if the skill author wrote `!`git diff``  in a project without git, the model should still see the body and decide what to do, not have the whole turn explode.

### 8b.4 Model-invokable vs. user-only skills

`disable-model-invocation: true` keeps the skill loaded but **not** registered as a callable tool — it doesn't appear in the model's tool catalog. The user can still invoke it directly via `/<skill-name>` in the CLI REPL ([cli/commands.py](src/botcircuits/cli/commands.py)), which calls the same `render_body()` and prints the result. Useful for "preset prompt" skills that should never be model-triggered (e.g. `/security-review`).

### 8b.5 `allowed-tools` hint

When set, `render_body()` appends a markdown footer to the rendered body: `Preferred tools for this skill: \`shell_exec\`, \`edit_file\``. This is a hint to the model, not an enforced restriction — the agent loop doesn't read it. Enforcing would require per-call tool filtering and tight coupling between skills and the registry; today the skill author's intent is communicated through the model's natural attention to the rendered text.

### 8b.6 Slash commands — `/skills` and `/<skill-name>`

- `/skills` lists every loaded filesystem skill, marking user-only ones with `[user-only]`.
- `/<skill-name>` invokes the skill directly — bypassing the model. The handler is the *same* one the model would have called, so user-invoke and model-invoke produce identical output.

---

## 9. Hosted Skills

The `SkillSpec` (from [agent/skill/spec.py](src/botcircuits/agent/skill/spec.py)) carries an Anthropic-style skill id (`xlsx`, `pptx`, `docx`, `pdf`). Each provider interprets it differently:

| Provider   | What `SkillSpec` does                                                                |
|------------|--------------------------------------------------------------------------------------|
| Anthropic  | Adds three beta headers, attaches `code_execution_20250825` tool, sets `container.skills` with the named skills |
| OpenAI     | Ignores `skill_id`. Any non-empty list enables `code_interpreter` with auto container |
| Gemini     | Ignores `skill_id`. Any non-empty list enables `Tool(code_execution=...)`             |

The "skill_id is Anthropic-only" leakage is intentional. Anthropic Skills are real, named, versioned bundles; OpenAI/Gemini just have a generic Python sandbox. Pretending they're equivalent would give callers false confidence. So we ship the abstraction that's honest: *"give me hosted code execution; if you're on Anthropic, here's which skill bundle to load."*

---

## 10. Streaming Pipeline

Two layers of streaming:

### 10.1 Provider-level streaming
`provider.stream(...)` is an async generator yielding two-tuples:

```python
("text_delta", "Hello")        # incremental text chunk
("text_delta", " world")
("final", LLMResponse(...))    # assembled response, exactly once
```

This intentionally surfaces only what every provider can guarantee. Tool-call argument deltas are interesting but lossy (they're partial JSON); we read final tool calls from the assembled response instead. This tradeoff buys reliability across providers.

### 10.2 Agent-level streaming
`Agent.chat_stream(...)` runs the multi-round loop and yields normalized `StreamEvent`s:

```python
StreamEvent(type="text_delta", text="...")
StreamEvent(type="tool_call", tool_call=ToolCall(...))
StreamEvent(type="tool_result", tool_call_id="...", text="...", is_error=False)
StreamEvent(type="turn_end")          # one provider round done; loop may continue
StreamEvent(type="done", text="...")  # entire user turn done
StreamEvent(type="error", text="...")
```

This is the API the FastAPI gateway and CLI consume. A consumer never has to know which provider it's talking to; the events are stable.

The `tool_result` events use `asyncio.as_completed` so a slow MCP query doesn't block surfacing fast ones. Result blocks are still appended to history in original order.

---

## 11. Configuration

Config is layered. Highest wins:

```
CLI flags  >  --config JSON  >  built-in defaults (with $LLM_PROVIDER as the provider default)
```

[cli/config.py](src/botcircuits/cli/config.py) implements this with two patterns:

### 11.1 Sentinel-None for CLI flags
Every CLI flag uses `default=None`. After parsing, `None` means "user didn't pass this," and only non-`None` values override the JSON file. This is the standard idiom for layered config in argparse — without it you can't distinguish "user explicitly typed `--max-tokens 4096`" from "argparse filled in the default."

### 11.2 `CLIConfig` dataclass
The resolved config is a single dataclass:

```python
@dataclass
class CLIConfig:
    provider: str = "anthropic"
    model: str | None = None
    system: str | None = None
    session: str | None = None
    stream: bool = True
    max_tokens: int = 4096
    max_steps: int = 10
    show_tool_results: bool = False
    mcp_servers: list[MCPServer] = field(default_factory=list)
    tools: dict[str, Any] = field(default_factory=dict)
    workflow: dict[str, Any] = field(default_factory=lambda: {"normalize": True})
```

`load_config_file(path)` reads `settings.json`, validates keys, rejects an `mcp_servers` block with a migration hint, converts `tools` into the per-tool dispatch dict, and `workflow` into a validated `{normalize: bool}` (unknown keys inside `workflow` are rejected). `resolve(file_values, cli_values)` does the merge. The `workflow` block has its own merge step that layers user overrides over the `{"normalize": True}` default, so a partial block doesn't wipe defaults for keys the user omitted.

MCP entries are loaded separately by `load_mcp_layers` from `mcp.json` files using `parse_mcp_servers_object`, which expects `{"servers": {<name>: {<fields>}}}`. `load_layered_settings` calls both loaders and injects the merged MCP server list onto the returned dict as `mcp_servers` before `resolve()` sees it — so the resolved `CLIConfig` shape is unchanged.

### 11.3 Mutation helpers
`add_mcp_server`, `remove_mcp_server`, `list_mcp_servers` are read-modify-write helpers used by the `mcp` CLI subcommands. They target one `mcp.json` file at a time and strip default-valued fields on write so the JSON stays minimal — adding a hosted server with no auth shows up as just `{"url": "..."}` under its server-name key (mode `hosted` is the default and is omitted; the name is the dict key, not a field). When you copy an entry by hand, omitting fields just means "use the default."

### 11.4 .env loading
[botcircuits/__init__.py](src/botcircuits/__init__.py) calls `python-dotenv`'s `load_dotenv()` at import time. Resolution: `BOTCIRCUITS_ENV_FILE` if set, else the nearest `.env` walking up from the cwd. **Existing process env always wins** so CI/production environments aren't overridden.

This runs unconditionally on package import — every entry point (CLI, gateway, library use) picks up `.env` without each one needing its own bootstrap. The `noqa` import in `main.py` is there only to trigger the side effect for that bare entry point.

### 11.5 Why JSON, not YAML/TOML?
Three reasons:
1. **Stdlib only.** Python ships `json`. `pyyaml` is an extra dep with a CVE history; `tomllib` is read-only.
2. **Round-trippable.** The `mcp add` / `mcp remove` subcommands need to mutate the file. JSON's lack of comments is annoying but the writer can serialize cleanly without trying to preserve hand-formatting. YAML round-tripping eats whitespace and reorders keys.
3. **Schemaable.** A JSON Schema for `settings.json` is straightforward if we ever want IDE completion.

The trade-off is no comments and no trailing commas. Worth it.

---

## 12. CLI

[cli/](src/botcircuits/cli/). Two surfaces:

### 12.1 Chat REPL (no subcommand)
`botcircuits-cli` with no subcommand drops into the chat REPL ([cli/app.py:amain](src/botcircuits/cli/app.py)):
- **Interactive vs piped.** `sys.stdin.isatty()` decides. Interactive prints colored prompts and offers slash commands; piped reads one message and exits, so it's usable in shell pipelines.
- **Async input.** `input()` is blocking, so it runs in an executor. The event loop stays free to drive MCP heartbeats, future background tasks, etc.
- **Tool events are visible.** When the agent decides on a tool call, the streaming text breaks and you see `▸ tool_call name(args)`. When the result arrives, `◂ result …`. Then the assistant prefix reprints and text resumes streaming.
- **Slash commands route around the model.** Implemented in [cli/commands.py](src/botcircuits/cli/commands.py). None of these call the LLM:
  - `/reset`, `/session [id]`, `/system <text>`, `/stream on|off`, `/tools`, `/help`, `/quit`
  - `/memory` — print the on-disk MEMORY.md / USER.md summary (see §8a.6)
  - `/skills` — list loaded filesystem skills
  - `/<skill-name>` — invoke a filesystem skill directly (bypasses the model; see §8b.6)
  - `/workflow add "<prompt>" [--name <wf>]` — lazy-load `build_workflow` and ask the model to author a *new* workflow with the given intent. When `--name` is supplied, that slug-safe value is threaded through to the model as the exact `name` to pass to `build_workflow`, which doubles as both the on-disk filename (`<wf>.json`) and the registered tool name; omit it to let the model pick a fresh slug. The parser validates the name against the same regex the tool enforces so bad slugs fail at the CLI, not after the LLM round-trip. As an alternative to the inline `"<prompt>"`, `--file <path.md>` reads the prompt from a UTF-8 (Markdown) file via `_read_prompt_file` in [cli_commands.py](src/botcircuits/agent/workflow/cli_commands.py) — useful for long or reusable prompts. `--file` and an inline prompt are mutually exclusive, and a missing/empty file fails at the CLI before any LLM round-trip.
  - `/workflow edit "<prompt>" --name <wf>` — lazy-load `build_workflow` and ask the model to *overwrite* the named workflow with the given edit request. Locates the source file first (by filename, then by `name` field) and refuses if it doesn't exist, so the model never has to guess the path.
- **Lazy slash triggers.** `LAZY_TOOL_TRIGGERS` in [cli/commands.py](src/botcircuits/cli/commands.py) maps `/workflow → build_workflow`. The handler calls `register_builtin(...)` to load the tool on first use, threads in an `on_built` callback that re-runs `register_workflows(...)` so new/edited workflows become callable on the very next turn without a CLI restart, and forwards the composed prompt to the model as a regular chat message. Adding a new lazy trigger (e.g. `/something → some_tool`) is one entry in the map.
- **No external deps for rendering.** Just `argparse`, `asyncio`, ANSI escapes ([cli/ansi.py](src/botcircuits/cli/ansi.py)). ANSI is auto-disabled on non-TTY or when `NO_COLOR` is set.

### 12.2 `mcp` subcommand
[cli/commands_mcp.py](src/botcircuits/cli/commands_mcp.py). Four sub-subcommands:

| Command | What it does |
|---|---|
| `mcp list` | Print servers from the config file |
| `mcp add <name> ...` | Insert a server entry; `--replace` to overwrite |
| `mcp remove <name>` | Drop a server by name |
| `mcp test <name>` | Connect to a local server, list its tools, disconnect |

All four require `--config` and exit 2 on user errors (duplicate name, unknown server, missing required field). `mcp test` only works for local servers (hosted ones run inside the provider, not in our process).

#### Argparse pitfalls hit during implementation
- **`dest="command"` collision.** Don't name a subparser dest `command`, since `mcp add --command npx` will clobber it. Renamed to `subcommand`.
- **Flag-like values.** `--args -y,...` is parsed as a flag because `-y` looks like a short option. We use `nargs='*'` and accept either `--args -y foo bar` or `--args=-y,foo,bar` (the latter via `_split_listish` which accepts both).

These are the classes of bug you only find by running the actual CLI; they're called out here so the next person doesn't reintroduce them.

### 12.3 `workflow` subcommand
[cli/commands_workflow.py](src/botcircuits/cli/commands_workflow.py). One sub-subcommand today:

| Command | What it does |
|---|---|
| `workflow build --name=<name>` | Compile NL `conditions` on the workflow's `agentAction` steps into `choices` + `flow.variables`. Rewrites the JSON file in place; idempotent. |

The subcommand reuses `load_cli_config(args)` and `make_provider(...)` from [cli/app.py](src/botcircuits/cli/app.py) so it picks the same provider/model the chat REPL would use — author-time inference (building) and runtime inference (Layer B normalization) stay on the same model. The import is deferred inside `_cmd_build` to avoid a circular import (app.py imports `commands_workflow`, which would otherwise re-import app.py at module load).

Exit codes: 0 on success, 2 if `--id` is missing or the workflow isn't found, 1 if the provider call fails or returns unusable output. The successful path prints `(updated <path>)` plus a one-line summary (`states processed: N | expressions: M | variables: K`).

---

## 13. FastAPI Gateway

[gateway/](src/botcircuits/gateway/). A thin wrapper:

- One `Agent` is built at startup via FastAPI's `lifespan`, reused for every request.
- `POST /chat` calls `agent.chat()`, returns JSON.
- `POST /chat/stream` calls `agent.chat_stream()` and serializes each `StreamEvent` as a Server-Sent Event.
- `POST /sessions/{id}/reset` drops a session.
- `GET /healthz` is a liveness check.

### 13.1 Sharing config with the CLI
The gateway honors `BOTCIRCUITS_CONFIG` (env var pointing at the same JSON the CLI uses). When set, `mcp_servers` and `tools` apply to the gateway too. Env vars (`LLM_PROVIDER`, `ANTHROPIC_MODEL`, etc.) still win over JSON values for backwards compatibility.

This deliberately reuses [cli/config.py](src/botcircuits/cli/config.py) — there's exactly one schema and one resolver. If we ever extract a `botcircuits.config` module the gateway will move with it.

### 13.2 Why SSE, not WebSockets?
For one-way server-to-client streaming, SSE is just HTTP — works through any proxy, browser support is universal, no framing protocol to debug. WebSockets only earn their complexity when you need bidirectional streams (interrupting an in-flight response, mid-turn human input). If you need that later, swap the route; the `Agent.chat_stream` API stays the same.

### 13.3 SSE format
Each event is:
```
event: <name>
data: <json-encoded payload>

```
Trailing blank line is part of the spec. We emit a leading `: ready\n\n` comment so any reverse proxy flushes headers before the first real event arrives. The `X-Accel-Buffering: no` header tells nginx not to coalesce events.

### 13.4 Concurrency model
FastAPI handles many requests in parallel on the same agent. Each session has its own lock, so concurrent requests targeting different sessions truly run in parallel; concurrent requests on the same session serialize. The Agent itself is stateless across sessions, so this scales linearly with sessions.

---

## 13a. Message Gateway

[gateway/messaging.py](src/botcircuits/gateway/messaging.py) + [gateway/channels/](src/botcircuits/gateway/channels/). The Hermes-style "one process drives every platform" layer that sits on top of the same `Agent` the JSON/SSE routes use.

### 13a.1 Roles
- **`Channel` ABC** ([channels/base.py](src/botcircuits/gateway/channels/base.py)) — every adapter exposes `name`, `start()`, `stop()`, `routes() -> APIRouter | None`, and `send(OutboundMessage)`. Inbound is platform-specific (HTTP webhook, scheduler tick); outbound is uniform.
- **`InboundMessage` / `OutboundMessage`** — normalized envelopes. `InboundMessage` carries `channel`, `external_chat_id`, `text`, optional `sender_id`, raw payload, and an optional per-message `system` override (used by cron jobs).
- **`MessageGateway`** ([messaging.py](src/botcircuits/gateway/messaging.py)) — owns a `{name: Channel}` registry, drives lifecycles, and implements `handle_inbound(msg)` and `dispatch(msg)`.

### 13a.2 Session keys
`session_key(msg) = f"{msg.channel}:{msg.external_chat_id}"`. Channel-namespacing prevents an `external_chat_id` collision across platforms from accidentally merging two unrelated chats — Slack channel `C0123` and a WhatsApp number that happens to render the same don't share state.

### 13a.3 Inbound flow
1. The channel's FastAPI route validates platform signatures/tokens and converts the body into `InboundMessage` objects.
2. The route calls `gateway.dispatch(msg)` (returns an `asyncio.Task`, route returns 200 immediately).
3. `handle_inbound` runs `agent.chat(text, session_id=key, system=...)`.
4. The reply is delivered through the *same* channel via `Channel.send(...)`.

The 2xx-immediately model matters: Slack retries within ~3s, Meta within ~5s, both with exponential backoff. Holding the connection while the agent thinks would trigger duplicate deliveries.

### 13a.4 The cron channel
Not really a channel — a scheduler dressed up as one so it goes through the same code path. `CronChannel._run` ticks every 60s; for each `CronJob` whose 5-field cron expression matches the current UTC minute, it synthesizes an `InboundMessage(channel="cron", external_chat_id=job.name, text=job.prompt)` and calls `gateway.handle_inbound`.

Two guards on the tick loop:
- `_last_fired_minute` per job — even if a tick overruns (e.g. the agent takes 90s), the next minute boundary only fires once.
- `asyncio.wait_for(self._stop.wait(), timeout=60)` rather than `asyncio.sleep` — lets `stop()` interrupt the wait cleanly during shutdown.

The cron expression engine ([`_cron_matches`](src/botcircuits/gateway/channels/cron.py)) is intentionally minimal — `*`, literals, `A-B`, `*/S`, comma lists. Day-of-week accepts both `0` and `7` for Sunday. No timezone field, no `@daily` macros, no day-of-month ⊕ day-of-week disjunction — every job we plan to run today fits inside this grammar.

Each job has its own conversation history (the session key is its name), so a daily summary job builds context over time. `deliver_to_channel` + `deliver_to_chat_id` let a cron job route its reply through a different channel — e.g. "every weekday at 9:00 UTC, ask the agent for a summary and post it to Slack channel C0123".

### 13a.5 Platform specifics worth knowing
- **WhatsApp** uses Meta's two-phase webhook: a GET with `hub.mode=subscribe` and `hub.verify_token` (echo back `hub.challenge`), then POST event payloads. Non-text messages (media, reactions, statuses) are silently dropped — the agent only handles text today.
- **Slack** uses **Socket Mode** (matching the Hermes Agent setup at https://hermes-agent.nousresearch.com/docs/user-guide/messaging/slack). On `Channel.start()` we call `auth.test` to cache the bot's own `user_id`, then open a WebSocket via `slack_sdk.socket_mode.aiohttp.SocketModeClient` using the app-level token (`xapp-…`, scope `connections:write`). Every inbound `SocketModeRequest` is ACK'd in `_on_request` *before* event processing so Slack never retries on our slow paths; only `events_api` envelopes carrying `event_callback` are unpacked into messages. We subscribe to the four Hermes-recommended bot events — `message.im`, `message.channels`, `message.groups`, `app_mention` — and filter out subtype messages, `bot_id`-bearing echoes, and our own bot's `user_id` to prevent reply loops. Outbound is `chat.postMessage` via `AsyncWebClient(token=bot_token)`. There is no inbound HTTP route, no signing-secret verification, and no public URL requirement.
- **Generic webhook** is a fallback for "anything else." Inbound is `POST {chat_id, text, sender_id?}` with a `Bearer` token (optional but recommended). Outbound to a configured URL is also optional — when absent, the channel becomes inbound-only and the agent's reply is logged but not sent anywhere, which is fine when a caller polls for replies via `/chat`-style routes or just wants fire-and-forget triggering.

### 13a.6 Configuration plumbing
[gateway/messaging_config.py](src/botcircuits/gateway/messaging_config.py) merges two sources: env vars (credentials) and `.botcircuits/messaging.json` (richer config like cron-job lists). A channel registers itself when its required credentials are all present; otherwise it's skipped with an `info` log line and the gateway still starts. Bad JSON or a missing required cron field raises at startup so typos surface there, not at the first cron tick.

### 13a.7 Lifespan integration
`lifespan` in [app.py](src/botcircuits/gateway/app.py) is the one place the gateway is built. The agent comes up first (so a channel can call it immediately if a webhook races startup), then channels are registered, their routers are mounted via `app.include_router(...)`, then `gateway.start()` opens HTTP clients and starts the cron loop. Shutdown reverses it: `gateway.stop()` cancels the cron task and closes channel clients before the agent itself is torn down.

---

## 14. Conversation Store

[agent/store.py](src/botcircuits/agent/store.py). `ConversationStore` is a `dict[str, Conversation]` plus per-conversation `asyncio.Lock`. That's the entire implementation. Sessions live for the life of the process.

To plug in persistence, subclass:

```python
class RedisStore(ConversationStore):
    def get_or_create(self, session_id, system=None):
        # load from redis if present, else create fresh
        ...
```

The Agent only calls `store.get_or_create(session_id, system)` and `store.reset(session_id)`, so the surface to override is small. The per-conversation lock must remain process-local even with a remote store, since it serializes turns within one process.

---

## 15. Capability Matrix

| Capability                       | Anthropic | OpenAI    | Gemini       |
|----------------------------------|-----------|-----------|--------------|
| Local Python tools               | ✅        | ✅        | ✅            |
| Hosted MCP (provider executes)   | ✅        | ✅        | ❌ (auto-promoted to local) |
| Local MCP (we execute)           | ✅        | ✅        | ✅            |
| Skills (named bundles)           | ✅        | n/a       | n/a          |
| Hosted code execution            | ✅        | ✅ (`code_interpreter`) | ✅ (`code_execution`) |
| Streaming                        | ✅        | ✅        | ✅            |
| Async                            | ✅        | ✅        | ✅            |

---

## 16. Extension Points

### Add a new provider
Subclass `LLMProvider`, implement `complete` and `stream`, drop the file in [providers/](src/botcircuits/providers/), add it to `providers/__init__.py`. Look at any existing provider for the shape — they're each ~100 lines. Most of the work is translating `Message` blocks to/from the vendor's wire format.

### Add a new transport for local MCP
[`LocalMCPManager._open_session`](src/botcircuits/agent/mcp.py) switches on `cfg.transport`. Add a new branch for the new transport (e.g. websocket), keep the same `read, write` interface.

### Add a built-in tool with config
See §8.3. One file in `builtins/`, one entry in `_BUILTINS`. JSON config flows in automatically.

### Add a tool that needs context (DB, user info, …)
Capture it in a closure when registering:

```python
def make_lookup_user(db):
    async def handler(args: dict) -> dict:
        return await db.users.find_one(args["user_id"])
    return handler

reg.register(LocalTool(name="lookup_user", description="...",
                       input_schema=..., handler=make_lookup_user(db)))
```

Do this in your own bootstrap code on top of `default_registry()`. Don't put DB connections into the JSON config.

### Add a tool that needs the surrounding conversation (last assistant text, etc.)
Take a second `context` arg on the handler. The registry inspects the signature and only passes the dict to handlers that accept it, so opting in is one keyword:

```python
async def handler(args: dict, context: dict | None = None) -> str:
    ctx = context or {}
    last = ctx.get("last_assistant_message", "")
    ...
```

The agent loop fills `context` per turn with `{last_assistant_message, session_id}`; add new keys in [agent/core.py](src/botcircuits/agent/core.py)'s `tool_context` dict and they become available to every context-aware handler with no further plumbing.

### Add streaming token usage
`LLMResponse.raw` carries each provider's native response object. Read `usage` off it inside the provider, attach to `LLMResponse` as a new field, then surface it as a new `StreamEvent` type. The Agent loop won't change.

### Persist conversations
Subclass `ConversationStore`. Override `get_or_create` and `reset`. Keep the `asyncio.Lock` on each `Conversation` (don't try to make it cross-process; serialize at the request layer instead). The memory snapshot is injected by the base class inside `get_or_create`; subclass implementations should preserve that call (or re-implement it) so persisted sessions still pick up `MEMORY.md` / `USER.md` at session creation.

### Add a filesystem skill
Drop a directory under `./skills/` (or `./.botcircuits/skills/`) containing a `SKILL.md` with `name` / `description` frontmatter and a markdown body. The agent picks it up on `start()` and exposes it as a tool named after the directory. See §8b for the SKILL.md format and dynamic substitution rules. No code change needed.

### Add or rename a persistent-memory target
Edit `_TARGETS` and `_file_for()` in [agent/memory.py](src/botcircuits/agent/memory.py), add a corresponding cap in `_cap_for()`, and update the `memory` tool's `enum` for `target` in [agent/tools/builtins/memory.py](src/botcircuits/agent/tools/builtins/memory.py). `render_for_system_prompt` will need a matching `<...>` wrapper. Keep the caps tight — the snapshot ships in every prompt.

### Add a new messaging channel
Subclass `Channel` ([gateway/channels/base.py](src/botcircuits/gateway/channels/base.py)), set `name`, implement `send(OutboundMessage)`, and optionally `routes()` (for HTTP-driven inbound) or `start()`/`stop()` (for polling adapters). Convert platform-native payloads into `InboundMessage` and hand them to `gateway.dispatch(...)`. Wire it into `messaging_config.load()` and `app.py`'s lifespan beside the existing channels. The agent doesn't need to change — it's just another session.

### Tool-search for many tools
When you have dozens of MCP tools, the registry passes all of them to the model on every turn — wasteful. Add a `tool_search` LocalTool that takes a query and returns the most relevant tool names. Pair with an `allowed_tools` filter to limit what's sent. Anthropic's hosted MCP supports `defer_loading`; OpenAI supports `allowed_tools` directly on hosted MCP entries.

---

## 17. Design Trade-offs Worth Naming

**Provider-specific niceties hidden by default.** Anthropic's prompt caching, OpenAI's structured outputs, Gemini's grounding — none are exposed in the unified interface. Add them as optional kwargs on specific provider constructors when you need them; they won't be portable, and that's fine.

**No retry / backoff in the core.** Hot loops over flaky providers belong in middleware, not the agent. Wrap the provider with whatever retry library you like.

**Tool name namespacing for local MCP.** `server__tool` rather than just `tool`. Solves disambiguation when two MCP servers expose the same tool name; small cost in prompt readability.

**Skills abstraction is honest, not symmetric.** OpenAI and Gemini don't have named skill bundles, so `SkillSpec.skill_id` is meaningful only on Anthropic. Better than pretending otherwise.

**`shell_exec` ships enabled.** The y/N confirmation per call, timeout, and output cap make it safe by construction — the user gates every call — and useful out of the box, with no policy guessing about which commands are "safe" and no false sense of sandboxing from a fixed cwd. Anyone who needs unattended runs flips `auto: true`; anyone who wants nothing has `"shell_exec": null`. Non-tty contexts auto-engage auto mode so the gateway works without ceremony.

**Tool parameters in JSON, tool implementations in code.** The JSON config can override `shell_exec`'s timeout/output/auto because those are policy. It cannot register a brand-new tool because that would mean either loading code paths from disk (security review goes out the window) or hand-writing JSON-schema-as-code (worse than just writing Python). Code stays in code, parameters stay in config.

**In-memory store as default.** Persistence is the user's call. The Agent is stateless across sessions, the store is a swappable interface, and adding Redis/Postgres is a small subclass.

**One JSON schema, two consumers.** The CLI and the gateway share `cli/config.py`. If the gateway grows its own config concerns we'll lift the module out of `cli/` rather than duplicate the schema.

---

## 18. Suggested Next Improvements

- **Token-usage events** in the stream; aggregate per-session.
- **JSON Schema for `settings.json`** so editors offer completion and red-squiggle on typos before runtime.
- **Tool approval gates** for local MCP write tools (confirm before delete/move). Could reuse OpenAI's `require_approval` semantics for parity.
- **Structured logging** of provider requests / responses with PII redaction.
- **Tests** — provider mocks for the Agent loop, plus integration tests against real APIs gated by env vars. Extra value in covering the layered config (`config.resolve` precedence) and the `mcp` CLI roundtrips.
- **Lift `cli/config.py` to `botcircuits/config.py`** once the gateway's config needs diverge from the CLI's.
- **`botcircuits-cli tool test <name> --argv ...`** — symmetric with `mcp test` for verifying tool config without launching a chat.
- **Variable normalization caching.** If the LLM retries with identical args (model jitter, network retry), Layer B currently re-runs. Hashing `(workflow_name, state_id, args, last_assistant_message_hash)` and short-circuiting on cache hit would cut latency for retries without changing behavior.
- **`allowedValues` on indexed variables.** Lets Layer A snap case-variant strings (`"Delivered"` → `"delivered"`) to a known enum without the risk of corrupting case-sensitive ids elsewhere.
- **Persistent cron job history.** Cron jobs currently share the in-memory `ConversationStore`, so a daily-summary job loses its context on restart. A persistent `ConversationStore` (Redis/SQLite) would let cron jobs build long-running state.
- **More inbound channels.** Telegram, Discord, SMS (Twilio), Email (IMAP poll). Each is one file under `gateway/channels/` plus a config block — the agent loop doesn't change.
- **Streaming replies through channels.** The message gateway currently calls `agent.chat`, not `agent.chat_stream`. Slack and WhatsApp both support edit-in-place; we could stream `text_delta` events into a single message and edit it as the agent thinks.
- **Richer normalizer context.** Currently Layer B sees `last_assistant_message` and `last_user_message`. Surfacing recent tool results or a longer message tail would let it extract variables from upstream tool outputs (a `get_order` blob, say) without requiring the LLM to copy them by hand. Cost: more tokens per normalization call, more cache invalidation surface.
- **Mid-session memory refresh.** Today the memory snapshot is frozen at session start to keep the prompt cache warm. A `/memory reload` slash command (or a `force_refresh: true` arg on the `memory` tool) could surface fresh content to the live session for cases where the user explicitly wants the trade-off (cache miss for immediate effect).
- **JSON Schema for SKILL.md frontmatter.** With multiple optional keys (`allowed-tools`, `disable-model-invocation`, future ones), a schema would let editors red-squiggle typos before the skill silently fails to load.
