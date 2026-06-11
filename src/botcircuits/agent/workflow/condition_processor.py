"""Convert natural-language conditions in a workflow's choice steps into
rule-engine expressions, and derive the set of variables those expressions
reference.

Uses the agent's pluggable `LLMProvider` so the same provider/model the
main Agent uses also powers indexing. Output is `choices[].expressionList[]`
entries the choice handler already understands (see `engine/handlers/choice.py`).
"""

from __future__ import annotations

import json
import re
from typing import Any

from botcircuits.providers.base import LLMProvider
from botcircuits.types import Message


# Mirrors the operators the choice handler supports
# (engine/handlers/choice.py::_evaluate_operator).
SUPPORTED_OPERATORS = [
    "is", "is not",
    "greater than", "greater than or equal",
    "less than", "less than or equal",
    "contains", "not contains",
    "starts with", "ends with",
    "is empty", "is not empty",
]


def _dedupe_conditions(conditions: list[dict]) -> list[dict]:
    seen: set[str] = set()
    result: list[dict] = []
    for c in conditions:
        key = f"{c.get('next') or ''}::{(c.get('condition') or '').strip()}"
        if key in seen:
            continue
        seen.add(key)
        result.append(c)
    return result


def _collect_condition_steps(flow: dict) -> list[dict]:
    """Find every agentAction/question step that carries natural-language
    `conditions` at the step root. Filters out empty entries and dedupes
    per step.

    Branching lives on `agentAction` and `question` steps (via
    `step.conditions` / `step.choices`) and is evaluated on re-entry,
    after the LLM has had a chance to fill variables (for a `question`
    step, after the user's reply lands).
    """
    steps = flow.get("steps") or {}
    entries: list[dict] = []
    for step_id, step in steps.items():
        if not isinstance(step, dict):
            continue
        if step.get("type") not in ("agentAction", "question", "systemAction"):
            continue
        raw = step.get("conditions")
        if not isinstance(raw, list):
            continue

        filtered = [
            c for c in raw
            if isinstance(c, dict)
            and isinstance(c.get("condition"), str)
            and c["condition"].strip() != ""
        ]
        deduped = _dedupe_conditions(filtered)
        step["conditions"] = deduped
        if deduped:
            entries.append({"stepId": step_id, "step": step})
    return entries


def _build_step_summary(flow: dict) -> str:
    """One line per step so the LLM has the surrounding context when
    choosing variable names."""
    steps = flow.get("steps") or {}
    lines: list[str] = []
    for step_id, step in steps.items():
        if not isinstance(step, dict):
            continue
        sc = step.get("settings") or {}
        parts = [f"id={step_id}", f"type={step.get('type', '')}"]
        if sc.get("name"):
            parts.append(f"name={sc['name']}")
        if sc.get("action"):
            parts.append(f"action={sc['action']}")
        if sc.get("intentPrompt"):
            parts.append(f"intentPrompt={sc['intentPrompt']}")
        lines.append(" | ".join(parts))
    return "\n".join(lines)


def _build_prompt(flow: dict, condition_entries: list[dict]) -> str:
    step_summary = _build_step_summary(flow)

    condition_lines: list[str] = []
    for entry in condition_entries:
        step_id = entry["stepId"]
        step = entry["step"]
        sc = step.get("settings") or {}
        action = sc.get("action") or sc.get("name") or step.get("type") or step_id
        condition_lines.append(f"step_id={step_id} (action: {action}):")
        for idx, c in enumerate(step.get("conditions") or []):
            condition_lines.append(
                f'  - idx={idx} condition="{c.get("condition", "")}" '
                f"next={c.get('next') or ''}"
            )

    operators = ", ".join(f'"{o}"' for o in SUPPORTED_OPERATORS)

    return "\n".join([
        "You convert natural-language branching conditions in a state "
        "machine into expressions evaluable by a rule engine.",
        "",
        f"Supported operators (use ONLY these): {operators}",
        "",
        "Expression syntax: `<variable_name> <operator> <value>`",
        "  - variable_name: snake_case identifier representing a fact "
        "captured earlier in the workflow.",
        "  - String literal values must be wrapped in single quotes, e.g. "
        "`readme_found is 'yes'`.",
        "  - Use `is empty` / `is not empty` with no value, e.g. "
        "`readme_found is not empty`.",
        "",
        "For each condition you must:",
        "  1. Choose a stable variable_name that represents the underlying "
        "fact (reuse the SAME variable_name across conditions that test "
        "the same fact).",
        "  2. Produce an expression using one of the supported operators.",
        "  3. Define each unique variable once with a clear description and "
        'dataType ("string", "boolean", "number").',
        "",
        "Workflow overview:",
        step_summary,
        "",
        "Conditions to convert:",
        "\n".join(condition_lines),
        "",
        "Respond with a JSON object (no commentary, no markdown fence) of "
        "the form:",
        "{",
        '  "expressions": [',
        '    { "step_id": "<step id>", "idx": <index>, '
        '"expCondition": "<expression>" }',
        "  ],",
        '  "variables": [',
        '    { "variableName": "<snake_case>", '
        '"dataType": "string|boolean|number", '
        '"description": "<short description>" }',
        "  ]",
        "}",
    ])


_EXPRESSION_RE = re.compile(
    r"^\s*(?P<var>[A-Za-z_][A-Za-z0-9_]*)\s+"
    r"(?P<op>is not empty|is empty|is not|is|greater than or equal|"
    r"greater than|less than or equal|less than|not contains|contains|"
    r"starts with|ends with)"
    r"(?:\s+(?P<val>.+))?\s*$"
)


def _parse_expression(exp: str) -> dict | None:
    """Parse `<variable> <operator> <value>` back into the
    `{variable, operator, value}` shape the local choice handler reads.
    Returns None if the expression doesn't match.
    """
    m = _EXPRESSION_RE.match(exp)
    if not m:
        return None
    op = m.group("op")
    val_raw = (m.group("val") or "").strip()
    if op in ("is empty", "is not empty"):
        value: Any = ""
    else:
        # Strip a single pair of surrounding single OR double quotes.
        if (len(val_raw) >= 2
                and val_raw[0] == val_raw[-1]
                and val_raw[0] in ("'", '"')):
            value = val_raw[1:-1]
        elif val_raw.lower() in ("true", "false"):
            value = val_raw.lower() == "true"
        else:
            try:
                value = int(val_raw)
            except ValueError:
                try:
                    value = float(val_raw)
                except ValueError:
                    value = val_raw
    return {"variable": m.group("var"), "operator": op, "value": value}


_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def _extract_json(raw: str) -> dict:
    """Strip code fences / leading prose and parse the first JSON object."""
    text = raw.strip()
    if text.startswith("```"):
        # Drop the opening ``` (and optional language tag) plus the closing ```.
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```\s*$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = _JSON_OBJECT_RE.search(text)
        if not m:
            raise
        return json.loads(m.group(0))


async def _ask_llm_for_json(provider: LLMProvider, prompt: str) -> str:
    """Call the provider with a strict-JSON system message and return the
    raw assistant text. We do NOT use streaming or tools — this is a single
    deterministic call."""
    system = (
        "You produce strict JSON that matches the requested schema. "
        "Do not include commentary, prose, or markdown code fences."
    )
    messages = [Message(role="user", blocks=[{"type": "text", "text": prompt}])]
    response = await provider.complete(
        system=system,
        messages=messages,
        tools=[],
        hosted_mcp=[],
        skills=[],
        max_tokens=4096,
    )
    return response.text


async def generate_expressions_and_variables(
    flow: dict,
    provider: LLMProvider,
) -> dict:
    """Mutate `flow` in place:

      - For each choice step with NL `conditions`, populate
        `expCondition` on each condition and build a `choices` array the
        runtime engine understands.
      - Write the aggregated variable catalogue to
        `flow['variables']`.

    Returns a small summary dict for the CLI to log.
    """
    condition_entries = _collect_condition_steps(flow)
    if not condition_entries:
        flow["variables"] = flow.get("variables") or []
        return {"steps_processed": 0, "expressions": 0, "variables": 0}

    prompt = _build_prompt(flow, condition_entries)
    raw = await _ask_llm_for_json(provider, prompt)
    try:
        parsed = _extract_json(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"LLM did not return valid JSON for condition indexing: {e}\n"
            f"Raw response: {raw[:500]}"
        ) from e

    expressions = parsed.get("expressions") if isinstance(parsed, dict) else None
    variables = parsed.get("variables") if isinstance(parsed, dict) else None
    if not isinstance(expressions, list):
        expressions = []
    if not isinstance(variables, list):
        variables = []

    exp_by_key: dict[str, str] = {}
    for e in expressions:
        if not isinstance(e, dict):
            continue
        sid = e.get("step_id")
        idx = e.get("idx")
        exp = e.get("expCondition")
        if sid is None or idx is None or not isinstance(exp, str):
            continue
        exp_by_key[f"{sid}::{idx}"] = exp.strip()

    missing: list[str] = []
    expression_count = 0
    for entry in condition_entries:
        step_id = entry["stepId"]
        step = entry["step"]
        # Replace, don't append — re-indexing the same file must be
        # idempotent. If the author wants hand-written `choices` to
        # survive, they shouldn't use NL `conditions` on that step.
        new_choices: list[dict] = []

        for idx, c in enumerate(step.get("conditions") or []):
            exp = exp_by_key.get(f"{step_id}::{idx}")
            if not exp:
                missing.append(f'{step_id}[{idx}] "{c.get("condition", "")}"')
                continue
            c["expCondition"] = exp
            parsed_exp = _parse_expression(exp)
            if parsed_exp is None:
                missing.append(
                    f'{step_id}[{idx}] unparseable expression {exp!r}'
                )
                continue
            new_choices.append({
                "operator": "AND",
                "expressionList": [parsed_exp],
                "next": c.get("next"),
            })
            expression_count += 1

        step["choices"] = new_choices

    if missing:
        raise RuntimeError(
            "LLM did not produce usable expressions for: "
            + ", ".join(missing)
        )

    # Aggregate variables, preserving first-seen order, dropping duplicates.
    # The author's existing `flow.variables` are seeded FIRST so hand-authored
    # variables always survive an index — including those referenced only by
    # hand-written `choices` (which the LLM indexer never sees and so never
    # re-declares). Without this, re-indexing silently drops them and the
    # runtime's Layer A/B normalization has no schema to coerce their slots
    # against, so those branches mis-fire. The author wins on a name collision:
    # they declared the dataType deliberately, and the indexer's guess for a
    # same-named variable shouldn't override it.
    seen_names: set[str] = set()
    aggregated: list[dict] = []
    for v in list(flow.get("variables") or []) + list(variables):
        if not isinstance(v, dict):
            continue
        name = v.get("variableName")
        if not isinstance(name, str) or not name or name in seen_names:
            continue
        seen_names.add(name)
        aggregated.append({
            "variableName": name,
            "dataType": v.get("dataType") or "string",
            "description": v.get("description") or "",
        })
    flow["variables"] = aggregated

    return {
        "steps_processed": len(condition_entries),
        "expressions": expression_count,
        "variables": len(aggregated),
    }


__all__ = [
    "SUPPORTED_OPERATORS",
    "generate_expressions_and_variables",
]
