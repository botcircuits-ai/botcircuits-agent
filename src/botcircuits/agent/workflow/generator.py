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


def _prompt(instructions: str, name: str, resources: str = "") -> str:
    return "\n".join([
        f"Produce a workflow named '{name}' from the process described below.",
        "",
        (("WORKSPACE RESOURCES the workflow can read/run (wire your resolvers, "
          "itemSource, and itemFacts to these EXACT paths):\n" + resources + "\n")
         if resources.strip() else ""),
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
        "- Keep step actions terse and imperative.",
        "",
        "DESIGN PRINCIPLE — PREFER DETERMINISM, NEVER ASK THE USER FOR DATA THAT "
        "EXISTS. The input data the process needs (the order, the applicant, "
        "inventory, blocklists, prices) lives in workspace FILES and SCRIPTS — "
        "the instructions describe the POLICY, but you must wire it to read "
        "those files/run those scripts, not converse:",
        "- Do NOT use a `question` step to gather, validate, or 'ask for' input "
        "that is in a file. A `question` PAUSES the whole workflow waiting on a "
        "human and is almost always wrong here. Use `question` ONLY when the "
        "instructions explicitly require asking a person something no file holds.",
        "- When a fact is a deterministic lookup (a value in a file, membership "
        "in a list, a number in a range), give its variable a `resolver` so the "
        "ENGINE computes it with NO AI call:",
        '    { "variableName": "header_status", "description": "...",',
        '      "resolver": { "kind": "enum_check", "source": {"file": '
        '"data/order.json", "path": "region"}, "allowed": ["US","EU"], '
        '"true": "valid", "false": "invalid" } }',
        "    resolver kinds: jsonpath {file,path}; enum_check {source,allowed,"
        "true,false}; file_membership {file,value_source,true,false,"
        "ignore_comments}; range {source,min,max,true,false}.",
        "- When the process decides an outcome for EVERY item in a list, use a "
        "`listDecision` step with `itemSource` {file, path} (the list) and "
        "`itemVariables` (the per-item facts its `conditions` test). If each "
        "item's facts come from running a script, add `itemFacts` so the ENGINE "
        "runs it per item with NO AI:",
        '    "itemFacts": { "kind": "exec", "command": ["python3", '
        '"bin/price.py", "{sku}", "{qty}"], "parse": "json", "derive": { '
        '"sku": {"from_item":"sku"}, "in_stock": {"from_output":"found"}, '
        '"total": {"from_output":"line_total","default":0}, "enough": '
        '{"ge":["output.stock","item.qty"]} } }',
        "    derive rules: from_item:<k>; from_output:<k>[,default]; literal:<v>;"
        " ge:[<ref>,<ref>] where a ref is 'item.x' / 'output.y' / a literal.",
        "- A listDecision may set `nullOn` {field:[decisionLabels]} to blank a "
        "field for certain outcomes (e.g. a rejected item has no total: "
        '{"line_total": ["reject"]}).',
        "",
        "So: read header/screening facts via resolvers, process line items via a "
        "listDecision with itemFacts — aim for a workflow that runs WITHOUT "
        "pausing and WITHOUT the model deciding outcomes itself.",
        "",
        "Process description:",
        instructions,
    ])


_MAX_ATTEMPTS = 3


async def generate_workflow(
    instructions: str,
    name: str,
    provider: LLMProvider,
    resources: str = "",
) -> dict:
    """Generate an intent-only workflow source dict from NL `instructions`.

    `resources` (optional) is a manifest of workspace files/scripts the workflow
    may read or run (e.g. where the input record lives, the data files, the
    pricer script) — supplied because the policy prose often names data files
    but not the exact path the runtime will find them at. Wiring resolvers /
    itemSource / itemFacts to these paths is what keeps the generated workflow
    deterministic instead of pausing to ask the user.

    Returns the parsed workflow JSON (ready to write to disk and then
    `workflow build`). Retries a few times because a model occasionally emits
    slightly malformed JSON; a re-roll usually fixes it. Raises RuntimeError if
    no attempt yields valid JSON of the expected shape."""
    prompt = _prompt(instructions, name, resources)
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
