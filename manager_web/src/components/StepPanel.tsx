"use client";

import { useEffect, useRef, useState } from "react";
import { PlusIcon, TrashIcon } from "@/components/icons";
import { type WorkflowDoc, type WorkflowStep } from "@/lib/api";
import { cx } from "@/lib/format";

/**
 * Side panel for the flow editor, organized into two collapsible groups:
 *
 *   1. "Workflow" — description + a searchable Steps dropdown to jump to / add
 *      a step.
 *   2. "Step settings" — the fields for the selected step (name, action,
 *      default next, conditions).
 *
 * Every mutation produces a NEW `WorkflowDoc` handed back via `onChange`, so
 * the parent's doc stays the single source of truth and the JSON view syncs.
 * The UI editor only authors `agentAction` today, so the step type is implicit
 * and not shown.
 */
export function StepPanel({
  doc,
  selectedStep,
  step,
  onSelectStep,
  onChange,
}: {
  doc: WorkflowDoc;
  selectedStep: string | null;
  step: WorkflowStep | null;
  onSelectStep: (id: string | null) => void;
  onChange: (doc: WorkflowDoc) => void;
}) {
  const steps = doc.flow?.steps ?? {};
  const stepIds = Object.keys(steps);

  const [workflowOpen, setWorkflowOpen] = useState(true);
  const [settingsOpen, setSettingsOpen] = useState(true);

  const mutate = (fn: (steps: Record<string, WorkflowStep>) => void) => {
    const nextSteps: Record<string, WorkflowStep> = JSON.parse(JSON.stringify(steps));
    fn(nextSteps);
    onChange({ ...doc, flow: { ...(doc.flow ?? {}), steps: nextSteps } });
  };

  const addStep = () => {
    let n = stepIds.length + 1;
    let id = `step_${n}`;
    while (steps[id]) id = `step_${++n}`;
    mutate((s) => {
      s[id] = { type: "agentAction", settings: { action: "" }, next: "", id };
    });
    onSelectStep(id);
    setSettingsOpen(true);
  };

  const deleteStep = (id: string) => {
    mutate((s) => {
      delete s[id];
      for (const v of Object.values(s)) {
        if (v.next === id) v.next = "";
        if (v.conditions) v.conditions = v.conditions.filter((c) => c.next !== id);
      }
    });
    onSelectStep(null);
  };

  return (
    <div className="rounded-2xl border border-border bg-surface h-full flex flex-col overflow-hidden">
      <div className="flex-1 overflow-y-auto">
        {/* Group 1: Workflow (description + searchable steps nav) */}
        <Group title="Workflow" open={workflowOpen} onToggle={() => setWorkflowOpen((o) => !o)}>
          <Field label="Description">
            <textarea
              value={doc.description ?? ""}
              onChange={(e) => onChange({ ...doc, description: e.target.value })}
              rows={2}
              placeholder="When to run this workflow"
              className="w-full resize-none rounded-lg border border-border bg-bg px-2 py-1.5 text-sm text-fg focus:outline-none focus:ring-2 focus:ring-brand/40"
            />
          </Field>

          <div className="mt-3">
            <div className="flex items-center justify-between mb-1">
              <label className="text-xs font-medium uppercase tracking-wide text-muted">
                Steps ({stepIds.length})
              </label>
              <button
                onClick={addStep}
                className="inline-flex items-center gap-1 text-xs text-brand-600 dark:text-brand-400 hover:bg-elevated rounded-md px-1.5 py-1"
              >
                <PlusIcon className="w-3.5 h-3.5" /> Add
              </button>
            </div>
            <StepSearchSelect
              stepIds={stepIds}
              selected={selectedStep}
              onSelect={(id) => {
                onSelectStep(id);
                setSettingsOpen(true);
              }}
            />
          </div>
        </Group>

        {/* Group 2: Step settings */}
        <Group
          title={selectedStep ? `Step settings · ${selectedStep}` : "Step settings"}
          open={settingsOpen}
          onToggle={() => setSettingsOpen((o) => !o)}
        >
          {!step || !selectedStep ? (
            <p className="text-sm text-muted">Select a step to edit, or add one.</p>
          ) : (
            <StepFields
              id={selectedStep}
              step={step}
              stepIds={stepIds}
              isStart={doc.flow?.start === selectedStep || step.type === "start"}
              onRename={(newId) => {
                if (!newId || newId === selectedStep || steps[newId]) return;
                mutate((s) => {
                  const old = s[selectedStep];
                  delete s[selectedStep];
                  s[newId] = { ...old, id: newId };
                  for (const v of Object.values(s)) {
                    if (v.next === selectedStep) v.next = newId;
                    if (v.conditions)
                      v.conditions = v.conditions.map((c) =>
                        c.next === selectedStep ? { ...c, next: newId } : c,
                      );
                  }
                });
                if (doc.flow?.start === selectedStep) {
                  onChange({ ...doc, flow: { ...(doc.flow ?? {}), start: newId } });
                }
                onSelectStep(newId);
              }}
              onUpdate={(patch) =>
                mutate((s) => {
                  s[selectedStep] = { ...s[selectedStep], ...patch };
                })
              }
              onDelete={() => deleteStep(selectedStep)}
            />
          )}
        </Group>
      </div>
    </div>
  );
}

/** A searchable (plain text-contains) dropdown over the step ids. */
function StepSearchSelect({
  stepIds,
  selected,
  onSelect,
}: {
  stepIds: string[];
  selected: string | null;
  onSelect: (id: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const ref = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, [open]);

  const q = query.trim().toLowerCase();
  const matches = q ? stepIds.filter((id) => id.toLowerCase().includes(q)) : stepIds;

  return (
    <div ref={ref} className="relative">
      <button
        onClick={() => setOpen((o) => !o)}
        className="w-full flex items-center justify-between rounded-lg border border-border bg-bg px-2 h-9 text-sm text-fg hover:bg-elevated"
      >
        <span className={cx("font-mono truncate", !selected && "text-muted")}>
          {selected ?? "Select a step…"}
        </span>
        <ChevronDown />
      </button>

      {open && (
        <div className="absolute z-20 mt-1 w-full rounded-lg border border-border bg-surface shadow-lg">
          <div className="p-2 border-b border-border">
            <input
              autoFocus
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Search steps…"
              className="w-full rounded-md border border-border bg-bg px-2 h-8 text-sm text-fg focus:outline-none focus:ring-2 focus:ring-brand/40"
            />
          </div>
          <div className="max-h-56 overflow-y-auto p-1">
            {matches.length === 0 && (
              <p className="px-2 py-1.5 text-xs text-muted">No matching steps.</p>
            )}
            {matches.map((id) => (
              <button
                key={id}
                onClick={() => {
                  onSelect(id);
                  setOpen(false);
                  setQuery("");
                }}
                className={cx(
                  "w-full text-left text-sm rounded-md px-2 py-1.5 font-mono truncate",
                  selected === id
                    ? "bg-brand/15 text-fg ring-1 ring-brand/30"
                    : "text-muted hover:text-fg hover:bg-elevated",
                )}
              >
                {id}
              </button>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

function StepFields({
  id,
  step,
  stepIds,
  isStart,
  onRename,
  onUpdate,
  onDelete,
}: {
  id: string;
  step: WorkflowStep;
  stepIds: string[];
  isStart: boolean;
  onRename: (id: string) => void;
  onUpdate: (patch: Partial<WorkflowStep>) => void;
  onDelete: () => void;
}) {
  const [idDraft, setIdDraft] = useState(id);
  useEffect(() => setIdDraft(id), [id]);
  const conditions = step.conditions ?? [];
  const targets = stepIds.filter((s) => s !== id);

  return (
    <div className="space-y-3">
      <Field label="Step name">
        <input
          value={idDraft}
          onChange={(e) => setIdDraft(e.target.value)}
          onBlur={() => onRename(idDraft.trim())}
          disabled={isStart}
          className={cx(
            "w-full rounded-lg border border-border bg-bg px-2 h-9 text-sm font-mono text-fg focus:outline-none focus:ring-2 focus:ring-brand/40",
            isStart && "opacity-60",
          )}
        />
      </Field>

      {!isStart && (
        <Field label="Action">
          <textarea
            value={step.settings?.action ?? ""}
            onChange={(e) =>
              onUpdate({ settings: { ...(step.settings ?? {}), action: e.target.value } })
            }
            rows={4}
            placeholder="Natural-language instruction for the agent"
            className="w-full resize-none rounded-lg border border-border bg-bg px-2 py-1.5 text-sm text-fg focus:outline-none focus:ring-2 focus:ring-brand/40"
          />
        </Field>
      )}

      <Field label="Default next (otherwise)">
        <select
          value={step.next ?? ""}
          onChange={(e) => onUpdate({ next: e.target.value })}
          className="w-full rounded-lg border border-border bg-bg px-2 h-9 text-sm text-fg focus:outline-none focus:ring-2 focus:ring-brand/40"
        >
          <option value="">— terminal —</option>
          {targets.map((t) => (
            <option key={t} value={t}>
              {t}
            </option>
          ))}
        </select>
      </Field>

      <div>
        <div className="flex items-center justify-between mb-1.5">
          <label className="text-xs font-medium uppercase tracking-wide text-muted">
            Conditions
          </label>
          <button
            onClick={() =>
              onUpdate({ conditions: [...conditions, { condition: "", next: "" }] })
            }
            className="inline-flex items-center gap-1 text-xs text-brand-600 dark:text-brand-400 hover:bg-elevated rounded-md px-1.5 py-1"
          >
            <PlusIcon className="w-3.5 h-3.5" /> Add
          </button>
        </div>
        <div className="space-y-2">
          {conditions.map((c, i) => (
            <div key={i} className="rounded-lg border border-border p-2 space-y-1.5">
              <input
                value={c.condition}
                onChange={(e) => {
                  const next = [...conditions];
                  next[i] = { ...c, condition: e.target.value };
                  onUpdate({ conditions: next });
                }}
                placeholder="natural-language test"
                className="w-full rounded-md border border-border bg-bg px-2 h-8 text-sm text-fg focus:outline-none focus:ring-2 focus:ring-brand/40"
              />
              <div className="flex items-center gap-1.5">
                <span className="text-xs text-muted">→</span>
                <select
                  value={c.next}
                  onChange={(e) => {
                    const next = [...conditions];
                    next[i] = { ...c, next: e.target.value };
                    onUpdate({ conditions: next });
                  }}
                  className="flex-1 rounded-md border border-border bg-bg px-2 h-8 text-sm text-fg focus:outline-none focus:ring-2 focus:ring-brand/40"
                >
                  <option value="">— pick step —</option>
                  {targets.map((t) => (
                    <option key={t} value={t}>
                      {t}
                    </option>
                  ))}
                </select>
                <button
                  onClick={() => onUpdate({ conditions: conditions.filter((_, j) => j !== i) })}
                  className="text-muted hover:text-danger rounded-md p-1"
                  aria-label="Remove condition"
                >
                  <TrashIcon className="w-4 h-4" />
                </button>
              </div>
            </div>
          ))}
          {conditions.length === 0 && (
            <p className="text-xs text-muted">
              No branches — this step always goes to its default next.
            </p>
          )}
        </div>
      </div>

      {!isStart && (
        <button
          onClick={onDelete}
          className="w-full inline-flex items-center justify-center gap-2 h-9 rounded-lg text-sm text-danger border border-danger/30 hover:bg-danger/10"
        >
          <TrashIcon className="w-4 h-4" /> Delete step
        </button>
      )}
    </div>
  );
}

/** A collapsible titled section. */
function Group({
  title,
  open,
  onToggle,
  children,
}: {
  title: string;
  open: boolean;
  onToggle: () => void;
  children: React.ReactNode;
}) {
  return (
    <div className="border-b border-border last:border-b-0">
      <button
        onClick={onToggle}
        className="w-full flex items-center gap-2 px-3 py-2.5 text-left hover:bg-elevated/50"
      >
        <ChevronDown className={cx("transition-transform", !open && "-rotate-90")} />
        <span className="text-sm font-medium text-fg truncate">{title}</span>
      </button>
      {open && <div className="px-3 pb-3">{children}</div>}
    </div>
  );
}

function ChevronDown({ className = "" }: { className?: string }) {
  return (
    <svg
      width="14"
      height="14"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      className={cx("text-muted shrink-0", className)}
    >
      <path d="m6 9 6 6 6-6" />
    </svg>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <label className="block text-xs font-medium uppercase tracking-wide text-muted mb-1">
        {label}
      </label>
      {children}
    </div>
  );
}
