# CLI

[← Implementation Guide index](../../IMPLEMENTATION.md)

---

## 12. CLI

[cli/](../../src/botcircuits/cli/). Two surfaces:

### 12.1 Chat REPL (no subcommand)
`botcircuits-cli` with no subcommand drops into the chat REPL ([cli/app.py:amain](../../src/botcircuits/cli/app.py)):
- **Interactive vs piped.** `sys.stdin.isatty()` decides. Interactive prints colored prompts and offers slash commands; piped reads one message and exits, so it's usable in shell pipelines.
- **Async input.** `input()` is blocking, so it runs in an executor. The event loop stays free to drive MCP heartbeats, future background tasks, etc.
- **Tool events are visible.** When the agent decides on a tool call, the streaming text breaks and you see `▸ tool_call name(args)`. When the result arrives, `◂ result …`. Then the assistant prefix reprints and text resumes streaming.
- **Slash commands route around the model.** Implemented in [cli/commands.py](../../src/botcircuits/cli/commands.py). None of these call the LLM:
  - `/reset`, `/session [id]`, `/system <text>`, `/stream on|off`, `/tools`, `/help`, `/quit`
  - `/memory` — print the on-disk MEMORY.md / USER.md summary (see §8a.6)
  - `/skills` — list loaded filesystem skills
  - `/<skill-name>` — invoke a filesystem skill directly (bypasses the model; see §8b.6)
  - `/workflow add "<prompt>" [--name <wf>]` — lazy-load `build_workflow` and ask the model to author a *new* workflow with the given intent. When `--name` is supplied, that slug-safe value is threaded through to the model as the exact `name` to pass to `build_workflow`, which doubles as both the on-disk filename (`<wf>.json`) and the registered tool name; omit it to let the model pick a fresh slug. The parser validates the name against the same regex the tool enforces so bad slugs fail at the CLI, not after the LLM round-trip. As an alternative to the inline `"<prompt>"`, `--file <path.md>` reads the prompt from a UTF-8 (Markdown) file via `_read_prompt_file` in [cli_commands.py](../../src/botcircuits/agent/workflow/cli_commands.py) — useful for long or reusable prompts. `--file` and an inline prompt are mutually exclusive, and a missing/empty file fails at the CLI before any LLM round-trip.
  - `/workflow edit "<prompt>" --name <wf>` — lazy-load `build_workflow` and ask the model to *overwrite* the named workflow with the given edit request. Locates the source file first (by filename, then by `name` field) and refuses if it doesn't exist, so the model never has to guess the path.
- **Lazy slash triggers.** `LAZY_TOOL_TRIGGERS` in [cli/commands.py](../../src/botcircuits/cli/commands.py) maps `/workflow → build_workflow`. The handler calls `register_builtin(...)` to load the tool on first use, threads in an `on_built` callback that re-runs `register_workflows(...)` so new/edited workflows become callable on the very next turn without a CLI restart, and forwards the composed prompt to the model as a regular chat message. Adding a new lazy trigger (e.g. `/something → some_tool`) is one entry in the map.
- **No external deps for rendering.** Just `argparse`, `asyncio`, ANSI escapes ([cli/ansi.py](../../src/botcircuits/cli/ansi.py)). ANSI is auto-disabled on non-TTY or when `NO_COLOR` is set.

### 12.2 `mcp` subcommand
[cli/commands_mcp.py](../../src/botcircuits/cli/commands_mcp.py). Four sub-subcommands:

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
[cli/commands_workflow.py](../../src/botcircuits/cli/commands_workflow.py). One sub-subcommand today:

| Command | What it does |
|---|---|
| `workflow build --name=<name>` | Compile NL `conditions` on the workflow's `agentAction`/`question` steps into `choices` + `flow.variables`. Rewrites the JSON file in place; idempotent. |

The subcommand reuses `load_cli_config(args)` and `make_provider(...)` from [cli/app.py](../../src/botcircuits/cli/app.py) so it picks the same provider/model the chat REPL would use — author-time inference (building) and runtime inference (Layer B normalization) stay on the same model. The import is deferred inside `_cmd_build` to avoid a circular import (app.py imports `commands_workflow`, which would otherwise re-import app.py at module load).

Exit codes: 0 on success, 2 if `--id` is missing or the workflow isn't found, 1 if the provider call fails or returns unusable output. The successful path prints `(updated <path>)` plus a one-line summary (`states processed: N | expressions: M | variables: K`).

---
