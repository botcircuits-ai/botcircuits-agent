"use client";

import { useState } from "react";
import { PlusIcon, TrashIcon } from "@/components/icons";
import {
  STEP_TYPE_AGENT_ACTION,
  SUPPORTED_STEP_TYPES,
  type WorkflowDoc,
  type WorkflowStep,
} from "@/lib/api";
import { cx } from "@/lib/format";

/**
 * Side panel for the flow editor: pick/add/delete a step and edit its fields.
 * Every mutation produces a NEW `WorkflowDoc` handed back via `onChange`, so
 * the parent's doc stays the single source of truth and the JSON view syncs.
 *
 * The UI editor only authors `agentAction` today (per the supported-type
 * constant); the type selector is kept so more step types can be added later
 * without a structural rewrite.
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
      s[id] = { type: STEP_TYPE_AGENT_ACTION, settings: { action: "" }, next: "", id };
    });
    onSelectStep(id);
  };

  const deleteStep = (id: string) => {
    mutate((s) => {
      delete s[id];
      // Clear dangling references so the graph/build stay consistent.
      for (const v of Object.values(s)) {
        if (v.next === id) v.next = "";
        if (v.conditions) v.conditions = v.conditions.filter((c) => c.next !== id);
      }
    });
    onSelectStep(null);
  };

  return (
    <div className="rounded-2xl border border-border bg-surface h-full flex flex-col">
      {/* Workflow meta */}
      <div className="p-3 border-b border-border space-y-2">
        <Field label="Description">
          <textarea
            value={doc.description ?? ""}
            onChange={(e) => onChange({ ...doc, description: e.target.value })}
            rows={2}
            placeholder="When to run this workflow"
            className="w-full resize-none rounded-lg border border-border bg-bg px-2 py-1.5 text-sm text-fg focus:outline-none focus:ring-2 focus:ring-brand/40"
          />
        </Field>
        <Field label="Start step">
          <select
            value={doc.flow?.start ?? ""}
            onChange={(e) =>
              onChange({ ...doc, flow: { ...(doc.flow ?? {}), start: e.target.value } })
            }
            className="w-full rounded-lg border border-border bg-bg px-2 h-9 text-sm text-fg focus:outline-none focus:ring-2 focus:ring-brand/40"
          >
            <option value="">—</option>
            {stepIds.map((id) => (
              <option key={id} value={id}>
                {id}
              </option>
            ))}
          </select>
        </Field>
      </div>

      {/* Step list */}
      <div className="p-3 border-b border-border">
        <div className="flex items-center justify-between mb-2">
          <span className="text-xs font-medium uppercase tracking-wide text-muted">
            Steps
          </span>
          <button
            onClick={addStep}
            className="inline-flex items-center gap-1 text-xs text-brand-600 dark:text-brand-400 hover:bg-elevated rounded-md px-1.5 py-1"
          >
            <PlusIcon className="w-3.5 h-3.5" /> Add
          </button>
        </div>
        <div className="space-y-1 max-h-40 overflow-y-auto">
          {stepIds.map((id) => (
            <button
              key={id}
              onClick={() => onSelectStep(id)}
              className={cx(
                "w-full text-left text-sm rounded-lg px-2 py-1.5 font-mono truncate",
                selectedStep === id
                  ? "bg-brand/15 text-fg ring-1 ring-brand/30"
                  : "text-muted hover:text-fg hover:bg-elevated",
              )}
            >
              {id}
            </button>
          ))}
        </div>
      </div>

      {/* Selected step editor */}
      <div className="p-3 flex-1 overflow-y-auto">
        {!step || !selectedStep ? (
          <p className="text-sm text-muted">Select a step to edit, or add one.</p>
        ) : (
          <StepFields
            id={selectedStep}
            step={step}
            stepIds={stepIds}
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
              // start ref
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
      </div>
    </div>
  );
}

function StepFields({
  id,
  step,
  stepIds,
  onRename,
  onUpdate,
  onDelete,
}: {
  id: string;
  step: WorkflowStep;
  stepIds: string[];
  onRename: (id: string) => void;
  onUpdate: (patch: Partial<WorkflowStep>) => void;
  onDelete: () => void;
}) {
  const [idDraft, setIdDraft] = useState(id);
  const conditions = step.conditions ?? [];
  const targets = stepIds.filter((s) => s !== id);

  return (
    <div className="space-y-3">
      <Field label="Step ID">
        <input
          value={idDraft}
          onChange={(e) => setIdDraft(e.target.value)}
          onBlur={() => onRename(idDraft.trim())}
          className="w-full rounded-lg border border-border bg-bg px-2 h-9 text-sm font-mono text-fg focus:outline-none focus:ring-2 focus:ring-brand/40"
        />
      </Field>

      <Field label="Type">
        <select
          value={step.type ?? STEP_TYPE_AGENT_ACTION}
          onChange={(e) => onUpdate({ type: e.target.value })}
          className="w-full rounded-lg border border-border bg-bg px-2 h-9 text-sm text-fg focus:outline-none focus:ring-2 focus:ring-brand/40"
        >
          {/* `start` is allowed for the entry step; agentAction is the only
              authorable action type today. More types come later. */}
          <option value="start">start</option>
          {SUPPORTED_STEP_TYPES.map((t) => (
            <option key={t} value={t}>
              {t}
            </option>
          ))}
        </select>
      </Field>

      {step.type !== "start" && (
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

      <button
        onClick={onDelete}
        className="w-full inline-flex items-center justify-center gap-2 h-9 rounded-lg text-sm text-danger border border-danger/30 hover:bg-danger/10"
      >
        <TrashIcon className="w-4 h-4" /> Delete step
      </button>
    </div>
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
