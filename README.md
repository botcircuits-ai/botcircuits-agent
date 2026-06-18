# botcircuits-agent

**Workflow-native automation, delivered as skills for the agent you already
use.** Install the BotCircuits skills into your agent (claude-code, hermes, …)
and then just talk to it:

```
claude > "create an order fulfillment workflow with stock check, ship, and backorder branches"
claude > "run order fulfillment"
```

Under the hood a BotCircuits workflow is a **deterministic state machine**: the
engine owns the control flow (branching, ordering, slot evaluation) and is
run-to-run predictable, while your agent supplies the LLM reasoning for each
step. The result is predictable, token-efficient multi-step automation — without
this project maintaining its own agent loop or LLM plumbing.

![botcircuits-agent-solution](docs/solution.png)

---

## How it works

BotCircuits ships **two skills** your agent loads:

| Skill | The user says… | The agent does… |
|---|---|---|
| **workflow-authoring** | _"create an order fulfillment workflow with …"_ | Writes the workflow JSON and builds it. |
| **workflow-running** | _"run order fulfillment"_ | Drives the deterministic engine, performing each step itself. |

When a workflow runs, the engine walks the state machine and hands each action
step back to your agent **in its current session** — your agent performs the
action with its own tools, reports what it observed, and the engine
deterministically decides the next step. The agent never picks the next step; it
just does the work the engine asks for, one step at a time.

This **inline / self** model means no nested process and no second model — the
agent that read the skill is the one that runs the workflow. (A different host
can run the same workflow over its CLI, or you can use the self-contained
[native agent](docs/native-agent.md); see [Runtime Providers](docs/concepts/11-runtime-providers.md).)

---

## Quick Start

### 1. Install the package

```bash
# Install uv if you don't have it: https://docs.astral.sh/uv/
git clone https://github.com/botcircuits-ai/botcircuits-agent
cd botcircuits-agent
uv venv --python 3.11 && source .venv/bin/activate
uv sync
```

The skills shell out to `python -m botcircuits.runtime.step_workflow`, so the
`botcircuits` package must be importable in your agent's environment — the step
above provides it. No LLM API key is needed: your host agent brings its own
model.

### 2. Install the skills into your agent

```bash
# Claude Code (personal scope, ~/.claude/skills) — the default
scripts/install-skills.sh

# Project scope, or another agent (e.g. Hermes):
scripts/install-skills.sh --target .claude/skills
scripts/install-skills.sh --target ~/.hermes/skills

# Develop against the repo (symlink instead of copy):
scripts/install-skills.sh --link
```

This copies `workflow-authoring` and `workflow-running` into the agent's skills
directory. Your agent now picks them up by description.

### 3. Use them in natural language

```
# Author
claude > "create an order fulfillment workflow: check stock; if all items are
          in stock, ship; otherwise create a backorder and notify the customer"

# Run
claude > "run order fulfillment for order #1024"
```

The authoring skill writes `.botcircuits/workflows/order_fulfillment.json` and
builds it; the running skill steps the agent through it, asking you for input
only when a `question` step needs it, and reporting the result at the end.

---

## Workflows

### Shape

A workflow is one JSON file under `.botcircuits/workflows/`:

```json
{
  "name": "order_fulfillment",
  "description": "Check stock, then ship or backorder.",
  "flow": {
    "start": "start",
    "steps": {
      "start": { "type": "start", "next": "check_stock" },
      "check_stock": {
        "type": "agentAction",
        "settings": { "action": "Check stock for the order items." },
        "next": "backorder",
        "conditions": [
          { "condition": "all items are in stock", "next": "ship" }
        ]
      },
      "ship":      { "type": "agentAction", "settings": { "action": "Ship the order." } },
      "backorder": { "type": "agentAction", "settings": { "action": "Create a backorder and notify the customer." } }
    }
  }
}
```

`name` is the identifier; it must match `^[a-zA-Z0-9_-]+$`. Step types are
`start`, `agentAction`, `question`, and `systemAction`. To branch, attach a
`conditions` list at the **step root** (a sibling of `type`/`next`, not nested
in `settings`); the step's own `next` is the default ("otherwise") branch.

### Build

The raw file is *not* what runs. **Building** compiles each natural-language
`condition` into a deterministic `choices[]` entry and emits an aggregated
`flow.variables` list, so the engine picks branches without re-calling the LLM:

```bash
botcircuits workflow build --name order_fulfillment
```

The runtime only loads from `.botcircuits/workflows/.build/`. The authoring
skill builds for you automatically.

### Where things live

- `.botcircuits/workflows/*.json` — your authored sources (override the dir with
  `BOTCIRCUITS_WORKFLOWS_DIR`).
- `.../.build/` — built, runnable copies.
- `.../.runs/` — transient pause/resume cursors (gitignored).

---

## Skills

A **skill** is a folder with a `SKILL.md` an agent reads from disk. BotCircuits
ships its functionality *as* skills:

```
skills/
├── workflow-authoring/SKILL.md
├── workflow-running/SKILL.md
└── botcircuits-faq/SKILL.md
```

`SKILL.md` frontmatter declares a `name` and a `description` (which the agent
uses to decide when to invoke it); `allowed-tools` (optional) restricts which
tools the skill may call. The same folders work in any agent that supports
Claude-Code-style filesystem skills.

---

## Native agent (self-contained, optional)

BotCircuits also ships a **complete standalone agent** — an LLM-driven CLI with
its own provider adapters (Anthropic / OpenAI / Gemini), MCP, persistent memory,
streaming, and a FastAPI gateway for WhatsApp / Slack / webhooks / cron. It's the
`native` runtime provider and the default fallback when no host agent is
detected.

Setup, CLI, tool-use modes, slash commands, MCP, built-in tools, and the message
gateway all live in **[docs/native-agent.md](docs/native-agent.md)**.

---

## Documentation

- [Concepts](docs/concepts/00-index.md) — a concept-level tour (incl. [Runtime Providers](docs/concepts/11-runtime-providers.md)).
- [Implementation Guide](IMPLEMENTATION.md) — architecture & internals (incl. [Runtime Providers](docs/developer-guide/14-runtime-providers.md)).
- [Native Agent](docs/native-agent.md) — the self-contained BotCircuits agent.

---

## License

Licensed under the Apache License, Version 2.0 — [LICENSE](LICENSE)

## Built by [BotCircuits](https://botcircuits.ai)
