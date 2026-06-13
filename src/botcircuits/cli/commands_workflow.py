"""`botcircuits-cli workflow ...` subcommands.

  workflow build --name <workflow_name>   Generate expressions + variables
                                          for the choice steps of a workflow
                                          and write them back to the file.
  workflow eval [--dataset <path>] [--repeats N] [--report <path>]
                                          Run the evaluation framework
                                          comparing the STM engine to a
                                          prompt-only baseline on the
                                          dataset's cases.

The builder uses the same LLM provider/model the main agent is configured
with so the inference quality (and API key wiring) matches chat behavior.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from botcircuits.agent.workflow.condition_processor import generate_expressions_and_variables
from botcircuits.agent.workflow.engine.segments import compute_segments
from botcircuits.agent.workflow.evaluation import (
    EvalDatasetError,
    discover_datasets,
    load_dataset,
    render_text,
    resolve_eval_dir,
    run_evaluation_datasets,
    write_json_report,
)
from botcircuits.agent.workflow.local import (
    DEFAULT_WORKFLOWS_DIR,
    LocalWorkflowError,
    WORKFLOWS_DIR_ENV,
    _resolve_build_dir,
    _resolve_workflows_dir,
)
from botcircuits.cli.ansi import C, out
from botcircuits.cli.config import ConfigError


# ---------------------------------------------------------------------------
# Subparser wiring
# ---------------------------------------------------------------------------


def add_workflow_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Wire the `workflow` command and its sub-subcommands.

    Mirrors the layout of `add_mcp_subparser` so the two read consistently.
    """
    wf = subparsers.add_parser(
        "workflow",
        help="Manage local BotCircuits workflows (build conditions, etc.)",
    )
    wf_subs = wf.add_subparsers(dest="workflow_cmd", required=True)

    build_p = wf_subs.add_parser(
        "build",
        help="Convert natural-language conditions into rule-engine "
             "expressions and emit flow.variables for the given workflow.",
    )
    # The positional fallback keeps `workflow build <name>` working too.
    build_p.add_argument(
        "--name", dest="workflow_name", default=None,
        help="Workflow name (matches the file's `name` field or its "
             "filename stem in the workflows directory).",
    )
    build_p.add_argument(
        "workflow_name_pos", nargs="?", default=None,
        help=argparse.SUPPRESS,
    )

    eval_p = wf_subs.add_parser(
        "eval",
        help="Run the workflow evaluation framework: compare the STM "
             "engine against a prompt-only baseline on the dataset's "
             "cases, scoring accuracy and consistency.",
    )
    eval_p.add_argument(
        "--dataset", dest="eval_dataset", default=None,
        help="Path to a single dataset JSON file. When omitted, every "
             "*.json file under $BOTCIRCUITS_EVAL_DIR (or "
             ".botcircuits/evaluation) is loaded.",
    )
    eval_p.add_argument(
        "--repeats", dest="eval_repeats", type=int, default=3,
        help="How many times to run each case for consistency scoring "
             "(default 3). The workflow engine is deterministic so "
             "extra repeats only stress the prompt-only baseline.",
    )
    eval_p.add_argument(
        "--report", dest="eval_report", default=None,
        help="Optional path to write the full JSON report to. The "
             "summary table is always printed to stdout.",
    )
    eval_p.add_argument(
        "--skip-prompt-baseline", dest="eval_skip_prompt",
        action="store_true",
        help="Run only the workflow side. Useful for fast local checks "
             "that don't want to spend LLM credits on the baseline.",
    )
    eval_p.add_argument(
        "--cleanup-inline-workflow", dest="eval_cleanup_inline",
        action="store_true",
        help="After running an inline-build dataset, delete the "
             "generated workflow files (source and indexed build "
             "artifact). Off by default — the generated files are "
             "left on disk so you can inspect what the LLM authored "
             "and re-run the eval against the same workflow without "
             "rebuilding. Pass this flag when you want each run to "
             "leave the project clean.",
    )


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def run_workflow_command(args: argparse.Namespace) -> int:
    """Entry point for `botcircuits-cli workflow ...`. Returns exit code."""
    if args.workflow_cmd == "build":
        return _cmd_build(args)
    if args.workflow_cmd == "eval":
        return _cmd_eval(args)

    out(C.red(f"[workflow] unknown subcommand: {args.workflow_cmd}"))
    return 2


# ---------------------------------------------------------------------------
# Subcommand bodies
# ---------------------------------------------------------------------------


def _cmd_build(args: argparse.Namespace) -> int:
    # Imported locally to dodge the app <-> commands_workflow circular import.
    from .app import load_cli_config, make_provider

    workflow_name = args.workflow_name or args.workflow_name_pos
    if not workflow_name:
        out(C.red("[workflow] `build` requires --name=<workflow name>"))
        return 2

    try:
        cfg = load_cli_config(args)
    except ConfigError as e:
        out(C.red(f"[config] {e}"))
        return 2

    try:
        source_path, record = _locate_workflow_file(workflow_name)
    except LocalWorkflowError as e:
        out(C.red(f"[workflow] {e}"))
        return 2

    flow = record.get("flow")
    if not isinstance(flow, dict):
        out(C.red(
            f"[workflow] {source_path} is missing flow; "
            f"nothing to index."
        ))
        return 2

    provider = make_provider(cfg.provider, cfg.model)
    out(C.dim(
        f"building {workflow_name!r} using provider={cfg.provider} "
        f"model={provider.model}"
    ))

    try:
        summary = asyncio.run(
            generate_expressions_and_variables(flow, provider)
        )
    except Exception as e:
        out(C.red(f"[workflow] build failed: {type(e).__name__}: {e}"))
        return 1
    else:
        # Branch-delimited segments are derived AFTER the indexer so the
        # `choices` it emits are present. The engine runner reads
        # `flow["segments"]` to batch consecutive non-branching steps into
        # one LLM call.
        flow["segments"] = compute_segments(flow)
    finally:
        # `make_provider` builds a fresh provider for this run; release any
        # async clients it opened.
        try:
            asyncio.run(provider.aclose())
        except Exception:
            pass

    build_dir = _resolve_build_dir()
    try:
        build_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        out(C.red(
            f"[workflow] failed to create build dir {build_dir}: "
            f"{type(e).__name__}: {e}"
        ))
        return 1

    # Mirror the source filename so `<name>.json` in the source aligns
    # with `<name>.json` in the build dir.
    build_path = build_dir / source_path.name
    _write_workflow(build_path, record)

    out(C.dim(
        f"  steps processed: {summary['steps_processed']}  |  "
        f"expressions: {summary['expressions']}  |  "
        f"variables: {summary['variables']}"
    ))
    out(C.dim(f"(source: {source_path})"))
    out(C.dim(f"(built:  {build_path})"))
    return 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _locate_workflow_file(workflow_name: str) -> tuple[Path, dict]:
    """Find the workflow.json file that matches `workflow_name`.

    Strategy:
      1. Try `<dir>/<workflow_name>.json` directly.
      2. Otherwise scan every `*.json` and match on its `name` field.
    """
    directory = _resolve_workflows_dir()
    if not directory.is_dir():
        raise LocalWorkflowError(
            f"workflows directory does not exist: {directory}. "
            f"Set ${WORKFLOWS_DIR_ENV} or create {DEFAULT_WORKFLOWS_DIR}/."
        )

    direct = directory / f"{workflow_name}.json"
    if direct.exists():
        return direct, _load_json(direct)

    for path in sorted(directory.glob("*.json")):
        try:
            data = _load_json(path)
        except LocalWorkflowError:
            continue
        if data.get("name") == workflow_name:
            return path, data

    raise LocalWorkflowError(
        f"no workflow with name {workflow_name!r} in {directory}"
    )


def _load_json(path: Path) -> dict:
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        raise LocalWorkflowError(f"failed to load {path}: {e}") from e
    if not isinstance(data, dict):
        raise LocalWorkflowError(
            f"{path} must be a JSON object at the top level"
        )
    return data


def _write_workflow(path: Path, record: dict) -> None:
    with path.open("w", encoding="utf-8") as f:
        json.dump(record, f, indent=2, ensure_ascii=False)
        f.write("\n")


# ---------------------------------------------------------------------------
# `workflow eval`
# ---------------------------------------------------------------------------


def _cmd_eval(args: argparse.Namespace) -> int:
    # Imported locally to dodge the app <-> commands_workflow circular import.
    from .app import load_cli_config, make_provider

    try:
        cfg = load_cli_config(args)
    except ConfigError as e:
        out(C.red(f"[config] {e}"))
        return 2

    # Load datasets. `--dataset` targets a single file; otherwise we
    # scan the configured eval directory.
    try:
        if args.eval_dataset:
            datasets = [load_dataset(Path(args.eval_dataset))]
        else:
            datasets = discover_datasets()
    except EvalDatasetError as e:
        out(C.red(f"[eval] {e}"))
        return 2

    case_count = sum(len(ds.cases) for ds in datasets)
    if not case_count:
        eval_dir = resolve_eval_dir()
        out(C.yellow(
            f"[eval] no cases found"
            f"{' in ' + str(eval_dir) if not args.eval_dataset else ''}. "
            f"Create a JSON dataset under {eval_dir} or pass --dataset."
        ))
        return 2

    # `--skip-prompt-baseline` only suppresses the prompt-only RUNNER.
    # The provider is still constructed and forwarded so inline-build
    # and Layer-B normalization stay available. The two-flag split lets
    # callers say "I want to measure just the workflow side, but it
    # still has to be the real workflow side" without forcing them to
    # stub out the LLM the engine depends on.
    inline_count = sum(1 for ds in datasets if ds.is_inline)
    needs_provider = inline_count > 0 or not args.eval_skip_prompt
    provider = make_provider(cfg.provider, cfg.model) if needs_provider else None

    if args.eval_skip_prompt:
        baseline_note = "prompt-only baseline skipped"
    else:
        baseline_note = f"baseline provider={cfg.provider} model={provider.model}"
    out(C.dim(
        f"running {case_count} case(s) across {len(datasets)} dataset(s) "
        f"({inline_count} inline) x {args.eval_repeats} repeat(s); "
        f"{baseline_note}"
    ))

    try:
        report = asyncio.run(run_evaluation_datasets(
            datasets,
            provider=provider,
            repeats=args.eval_repeats,
            run_prompt_baseline=not args.eval_skip_prompt,
            cleanup_inline_workflow=args.eval_cleanup_inline,
        ))
    except Exception as e:
        out(C.red(f"[eval] failed: {type(e).__name__}: {e}"))
        return 1
    finally:
        if provider is not None:
            try:
                asyncio.run(provider.aclose())
            except Exception:
                pass

    out(render_text(report))
    if args.eval_report:
        report_path = Path(args.eval_report)
        try:
            write_json_report(report, report_path)
            out(C.dim(f"(report: {report_path})"))
        except OSError as e:
            out(C.red(
                f"[eval] failed to write report {report_path}: "
                f"{type(e).__name__}: {e}"
            ))
            return 1
    return 0
