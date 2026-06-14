"""NL → workflow generator — author a workflow SOURCE from plain instructions.

This is the front of the authoring pipeline: given a natural-language
description of a process (e.g. a use case's `Instructions.md`), it produces a
*draft, intent-only* workflow source file — the same shape an advanced user
would hand-write — which `workflow build` then compiles and optimizes.

    instructions (NL text)  ──►  generate_workflow(...)  ──►  <name>.json
                                  (one LLM call)              (intent-only source)
                                                              then `workflow build`

The generator deliberately emits ONLY intent (steps, NL `conditions`, variable
names + descriptions, optional `resolver` / listDecision `itemSource`+`itemFacts`
when the instructions describe a deterministic file/script lookup). It does NOT
emit compiled mechanics (`choices`/`expressionList`, `dataType`, `segments`,
`flow.result`, the `deterministic` flag) — `workflow build` generates those.

It is best-effort: the produced draft is meant to be reviewed and is then run
through the normal build, which validates it. A malformed LLM response raises so
the caller can surface it.
"""

from __future__ import annotations

import json

from botcircuits.providers.base import LLMProvider
from botcircuits.types import Message

from .condition_processor import _extract_json

_SYSTEM = (
    "You are a workflow author. You convert a natural-language description of a "
    "repeatable process into a BotCircuits workflow SOURCE file (JSON). Emit "
    "ONLY intent — never compiled mechanics. Return strict JSON, no prose, no "
    "markdown fences."
)


def _prompt(instructions: str, name: str) -> str:
    return "\n".join([
        f"Produce a workflow named '{name}' from the process described below.",
        "",
        "OUTPUT a JSON object of this shape (intent only):",
        "{",
        '  "name": "' + name + '",',
        '  "description": "<one line: what it does and when to run it>",',
        '  "flow": {',
        '    "start": "<first step id>",',
        '    "variables": [',
        '      { "variableName": "<snake_case>", "description": "<plain '
        'language; state the exact value words if it is a fixed set>" }',
        "    ],",
        '    "steps": {',
        '      "<step_id>": {',
        '        "type": "agentAction | question | listDecision",',
        '        "settings": { "action": "<plain-language instruction>" },',
        '        "conditions": [ { "condition": "<plain-language branch '
        'rule>", "next": "<step id or outcome>" } ],',
        '        "next": "<default next step or outcome>"',
        "      }",
        "    }",
        "  }",
        "}",
        "",
        "RULES:",
        "- Write branch logic as natural-language `conditions` (the builder "
        "compiles them). Do NOT write choices/expressionList/expCondition.",
        "- Do NOT write dataType, segments, flow.result, or a `deterministic` "
        "flag — the builder fills those.",
        "- Use a `listDecision` step when the process decides an outcome for "
        "EVERY item in a list (e.g. each order line, each applicant). Give it "
        "`itemSource` {file, path} for the list and `itemVariables` (the "
        "per-item facts its conditions test).",
        "- When a fact is a deterministic lookup the engine can do without AI "
        "(a value in a file, membership in a list, a number range, or running a "
        "script per item), express it: a variable may carry a `resolver` "
        "({kind: jsonpath|enum_check|file_membership|range, ...}); a "
        "listDecision may carry `itemFacts` ({kind:'exec', command:[...with "
        "{field} placeholders], parse:'json', derive:{fact: rule}}). Only do "
        "this when the instructions clearly describe such a file/script.",
        "- Keep step actions terse and imperative.",
        "",
        "Process description:",
        instructions,
    ])


_MAX_ATTEMPTS = 3


async def generate_workflow(
    instructions: str,
    name: str,
    provider: LLMProvider,
) -> dict:
    """Generate an intent-only workflow source dict from NL `instructions`.

    Returns the parsed workflow JSON (ready to write to disk and then
    `workflow build`). Retries a few times because a model occasionally emits
    slightly malformed JSON; a re-roll usually fixes it. Raises RuntimeError if
    no attempt yields valid JSON of the expected shape."""
    prompt = _prompt(instructions, name)
    last_err = ""
    last_raw = ""
    for attempt in range(_MAX_ATTEMPTS):
        messages = [Message(role="user", blocks=[{"type": "text", "text": prompt}])]
        # On a retry, tell the model the previous output failed to parse.
        if attempt > 0:
            messages.append(Message(role="user", blocks=[{"type": "text", "text": (
                "Your previous response was not valid JSON "
                f"({last_err}). Return ONLY the corrected, strict JSON object — "
                "no prose, no markdown fences.")}]))
        resp = await provider.complete(
            system=_SYSTEM, messages=messages, tools=[], hosted_mcp=[],
            skills=[], max_tokens=8192,
        )
        last_raw = resp.text
        try:
            doc = _extract_json(resp.text)
        except (json.JSONDecodeError, ValueError) as e:
            last_err = str(e)
            continue
        if isinstance(doc, dict) and isinstance(doc.get("flow"), dict):
            doc["name"] = name  # force the requested name (file + tool name)
            return doc
        last_err = "output missing a `flow` object"
    raise RuntimeError(
        f"generator did not return valid workflow JSON after "
        f"{_MAX_ATTEMPTS} attempts: {last_err}\nLast raw: {last_raw[:500]}"
    )
