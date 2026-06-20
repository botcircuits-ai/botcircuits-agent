"use client";

import { useCallback, useState } from "react";
import { useRouter } from "next/navigation";
import { AuthoringChat } from "@/components/AuthoringChat";
import { StepPanel } from "@/components/StepPanel";
import { WorkflowGraph } from "@/components/WorkflowGraph";
import { CodeIcon, SparkleIcon, WorkflowIcon } from "@/components/icons";
import {
  api,
  STEP_TYPE_AGENT_ACTION,
  type BuildResult,
  type WorkflowDoc,
  type WorkflowStep,
} from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { cx } from "@/lib/format";

type Mode = "flow" | "json";

function emptyDoc(name: string): WorkflowDoc {
  return {
    name,
    description: "",
    flow: {
      start: "start",
      steps: {
        start: { type: "start", next: "", id: "start" },
      },
    },
  };
}

/**
 * Workflow create/edit view. Two synced modes over a single `WorkflowDoc`
 * source of truth:
 *   - "flow": ReactFlow canvas + per-step side panel.
 *   - "json": raw JSON textarea.
 * Both edit the same `doc` state, so switching modes never loses changes.
 * A natural-language authoring chat (right rail) can replace the whole doc.
 */
export function WorkflowEditor({
  initialName,
  initialDoc,
  isNew,
}: {
  initialName: string;
  initialDoc: WorkflowDoc | null;
  isNew: boolean;
}) {
  const { token, signOut } = useAuth();
  const router = useRouter();

  const [name, setName] = useState(initialName);
  const [doc, setDoc] = useState<WorkflowDoc>(
    initialDoc ?? emptyDoc(initialName || "new_workflow"),
  );
  const [mode, setMode] = useState<Mode>("flow");
  const [showChat, setShowChat] = useState(isNew);
  const [selectedStep, setSelectedStep] = useState<string | null>(null);

  // JSON-mode buffer + parse error. We keep the raw text separate so an
  // in-progress (temporarily invalid) edit isn't discarded; on every valid
  // parse we push it back into `doc` so the flow view stays in sync.
  const [jsonText, setJsonText] = useState(() => JSON.stringify(doc, null, 2));
  const [jsonError, setJsonError] = useState<string | null>(null);

  const [saving, setSaving] = useState(false);
  const [building, setBuilding] = useState(false);
  const [banner, setBanner] = useState<{ kind: "ok" | "err"; text: string } | null>(
    null,
  );
  const [buildOutput, setBuildOutput] = useState<BuildResult | null>(null);

  const nameValid = /^[a-zA-Z0-9_-]+$/.test(name);

  // Replace the whole doc (from JSON edits, the side panel, or AI authoring)
  // and refresh the JSON buffer so both modes reflect it.
  const applyDoc = useCallback((next: WorkflowDoc) => {
    setDoc(next);
    setJsonText(JSON.stringify(next, null, 2));
    setJsonError(null);
  }, []);

  // Switching INTO json: serialize current doc. Switching FROM json: try to
  // commit the buffer (block the switch if it's invalid).
  const switchMode = useCallback(
    (next: Mode) => {
      if (next === mode) return;
      if (mode === "json") {
        try {
          const parsed = JSON.parse(jsonText);
          setDoc(parsed);
          setJsonError(null);
        } catch (e: any) {
          setJsonError(e?.message ?? "Invalid JSON");
          return;
        }
      } else {
        setJsonText(JSON.stringify(doc, null, 2));
      }
      setMode(next);
    },
    [mode, jsonText, doc],
  );

  const onJsonChange = useCallback((text: string) => {
    setJsonText(text);
    try {
      const parsed = JSON.parse(text);
      setDoc(parsed);
      setJsonError(null);
    } catch (e: any) {
      setJsonError(e?.message ?? "Invalid JSON");
    }
  }, []);

  const onAuthored = useCallback(
    (authored: WorkflowDoc) => {
      applyDoc(authored);
      if (authored.name && typeof authored.name === "string") setName(authored.name);
      setBanner({ kind: "ok", text: "Workflow generated and synced into the editor." });
    },
    [applyDoc],
  );

  const steps = doc.flow?.steps ?? {};
  const selected = selectedStep ? steps[selectedStep] : null;

  // Pending "create new step?" prompt raised by a drag-to-empty-canvas; holds
  // the source step the new node would be connected from.
  const [pendingNewFrom, setPendingNewFrom] = useState<string | null>(null);

  // --- Graph mutation helpers (canvas edits) -------------------------------
  // All produce a new doc through applyDoc so the JSON view stays in sync.
  const mutateSteps = useCallback(
    (fn: (steps: Record<string, WorkflowStep>) => Partial<WorkflowDoc> | void) => {
      const nextSteps: Record<string, WorkflowStep> = JSON.parse(
        JSON.stringify(doc.flow?.steps ?? {}),
      );
      const extra = fn(nextSteps) || {};
      applyDoc({ ...doc, ...extra, flow: { ...(doc.flow ?? {}), steps: nextSteps } });
    },
    [doc, applyDoc],
  );

  const renameStep = useCallback(
    (oldId: string, newId: string) => {
      if (!newId || newId === oldId) return;
      const cur = doc.flow?.steps ?? {};
      if (cur[newId]) return; // name collision — ignore
      const nextSteps: Record<string, WorkflowStep> = JSON.parse(JSON.stringify(cur));
      const old = nextSteps[oldId];
      if (!old) return;
      delete nextSteps[oldId];
      nextSteps[newId] = { ...old, id: newId };
      for (const v of Object.values(nextSteps)) {
        if (v.next === oldId) v.next = newId;
        if (v.conditions)
          v.conditions = v.conditions.map((c) =>
            c.next === oldId ? { ...c, next: newId } : c,
          );
      }
      const start = doc.flow?.start === oldId ? newId : doc.flow?.start;
      applyDoc({ ...doc, flow: { ...(doc.flow ?? {}), start, steps: nextSteps } });
      if (selectedStep === oldId) setSelectedStep(newId);
    },
    [doc, applyDoc, selectedStep],
  );

  const updateAction = useCallback(
    (id: string, action: string) =>
      mutateSteps((s) => {
        if (s[id]) s[id] = { ...s[id], settings: { ...(s[id].settings ?? {}), action } };
      }),
    [mutateSteps],
  );

  const updateEdgeCondition = useCallback(
    (from: string, kind: "default" | "condition", condIndex: number, condition: string) => {
      mutateSteps((s) => {
        const step = s[from];
        if (!step) return;
        if (kind === "condition" && step.conditions?.[condIndex]) {
          const conditions = [...step.conditions];
          conditions[condIndex] = { ...conditions[condIndex], condition };
          s[from] = { ...step, conditions };
        } else if (kind === "default") {
          // Editing the default ("otherwise") edge: setting a condition converts
          // it into a regular branch, leaving the step with no default path
          // (the backend tolerates that). Clearing it again leaves it default.
          if (condition.trim() && step.next) {
            const conditions = [...(step.conditions ?? []), { condition, next: step.next }];
            s[from] = { ...step, conditions, next: "" };
          }
        }
      });
    },
    [mutateSteps],
  );

  // Connect two existing steps: fill the default `next` if empty, otherwise add
  // a new (empty-condition) branch the user can then label inline.
  const connectSteps = useCallback(
    (from: string, to: string) => {
      if (from === to) return;
      mutateSteps((s) => {
        const step = s[from];
        if (!step || !s[to]) return;
        if (!step.next) {
          s[from] = { ...step, next: to };
        } else if (step.next === to) {
          // already the default — nothing to do
        } else {
          const conditions = [...(step.conditions ?? [])];
          if (!conditions.some((c) => c.next === to)) {
            conditions.push({ condition: "", next: to });
          }
          s[from] = { ...step, conditions };
        }
      });
    },
    [mutateSteps],
  );

  // Drag ended on empty canvas — ask before creating.
  const requestNewStep = useCallback((from: string) => setPendingNewFrom(from), []);

  const createConnectedStep = useCallback(() => {
    const from = pendingNewFrom;
    setPendingNewFrom(null);
    if (!from) return;
    const cur = doc.flow?.steps ?? {};
    let n = Object.keys(cur).length + 1;
    let id = `step_${n}`;
    while (cur[id]) id = `step_${++n}`;
    mutateSteps((s) => {
      s[id] = { type: STEP_TYPE_AGENT_ACTION, settings: { action: "" }, next: "", id };
      const step = s[from];
      if (step) {
        if (!step.next) s[from] = { ...step, next: id };
        else s[from] = { ...step, conditions: [...(step.conditions ?? []), { condition: "", next: id }] };
      }
    });
    setSelectedStep(id);
  }, [pendingNewFrom, doc, mutateSteps]);

  const save = useCallback(async () => {
    if (!token) return;
    if (!nameValid) {
      setBanner({ kind: "err", text: "Name must match ^[a-zA-Z0-9_-]+$." });
      return;
    }
    if (mode === "json" && jsonError) {
      setBanner({ kind: "err", text: "Fix the JSON before saving." });
      return;
    }
    let toSave = doc;
    if (mode === "json") {
      try {
        toSave = JSON.parse(jsonText);
      } catch {
        setBanner({ kind: "err", text: "Fix the JSON before saving." });
        return;
      }
    }
    setSaving(true);
    setBanner(null);
    try {
      const saved = await api.saveWorkflow(token, name, { ...toSave, name });
      applyDoc(saved);
      setBanner({ kind: "ok", text: "Saved." });
      if (isNew) router.replace(`/workflows/${encodeURIComponent(name)}`);
    } catch (err: any) {
      if (err?.status === 401) return signOut();
      setBanner({ kind: "err", text: err?.message ?? "Save failed" });
    } finally {
      setSaving(false);
    }
  }, [token, nameValid, mode, jsonError, doc, jsonText, name, applyDoc, isNew, router, signOut]);

  const build = useCallback(async () => {
    if (!token || !nameValid) return;
    setBuilding(true);
    setBanner(null);
    setBuildOutput(null);
    try {
      // Persist first so the build compiles the latest source.
      await save();
      const result = await api.buildWorkflow(token, name);
      setBuildOutput(result);
      setBanner(
        result.ok
          ? { kind: "ok", text: "Built — the workflow is now runnable." }
          : { kind: "err", text: "Build failed. See output below." },
      );
    } catch (err: any) {
      if (err?.status === 401) return signOut();
      setBanner({ kind: "err", text: err?.message ?? "Build failed" });
    } finally {
      setBuilding(false);
    }
  }, [token, nameValid, name, save, signOut]);

  return (
    <div className="flex flex-col h-[calc(100vh-3rem)]">
      {/* Header / toolbar */}
      <div className="shrink-0 flex flex-wrap items-center gap-3 pb-4 border-b border-border">
        <div className="flex items-center gap-2 min-w-0">
          <WorkflowIcon className="w-5 h-5 text-brand-600 dark:text-brand-400 shrink-0" />
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            disabled={!isNew}
            placeholder="workflow_name"
            className={cx(
              "font-mono text-sm rounded-lg border bg-bg px-3 h-9 text-fg focus:outline-none focus:ring-2 focus:ring-brand/40",
              nameValid ? "border-border" : "border-danger/50",
              !isNew && "opacity-70",
            )}
          />
        </div>

        <ModeToggle mode={mode} onChange={switchMode} />

        <div className="flex-1" />

        <button
          onClick={() => setShowChat((s) => !s)}
          className={cx(
            "inline-flex items-center gap-2 h-9 px-3 rounded-lg text-sm",
            showChat
              ? "bg-brand/15 text-fg ring-1 ring-brand/30"
              : "text-muted hover:text-fg hover:bg-elevated",
          )}
        >
          <SparkleIcon className="w-[18px] h-[18px]" />
          AI
        </button>
        <button
          onClick={save}
          disabled={saving}
          className="h-9 px-3 rounded-lg text-sm font-medium border border-border text-fg hover:bg-elevated disabled:opacity-50"
        >
          {saving ? "Saving…" : "Save"}
        </button>
        <button
          onClick={build}
          disabled={building}
          className="h-9 px-3 rounded-lg text-sm font-medium bg-brand text-white hover:bg-brand-600 disabled:opacity-50"
        >
          {building ? "Building…" : "Save & Build"}
        </button>
      </div>

      {banner && (
        <div
          className={cx(
            "shrink-0 mt-3 rounded-xl border px-4 py-2 text-sm",
            banner.kind === "ok"
              ? "border-ok/30 bg-ok/10 text-ok"
              : "border-danger/30 bg-danger/10 text-danger",
          )}
        >
          {banner.text}
        </div>
      )}

      {/* Body */}
      <div className="flex-1 min-h-0 mt-3 flex gap-3">
        <div className="flex-1 min-w-0 flex gap-3">
          {mode === "flow" ? (
            <>
              <div className="flex-1 min-w-0">
                <WorkflowGraph
                  doc={doc}
                  selectedStep={selectedStep}
                  onSelectStep={setSelectedStep}
                  onRenameStep={renameStep}
                  onUpdateAction={updateAction}
                  onUpdateEdgeCondition={updateEdgeCondition}
                  onConnect={connectSteps}
                  onConnectToEmpty={requestNewStep}
                />
              </div>
              <div className="w-[320px] shrink-0 overflow-y-auto">
                <StepPanel
                  doc={doc}
                  selectedStep={selectedStep}
                  step={selected ?? null}
                  onSelectStep={setSelectedStep}
                  onChange={applyDoc}
                />
              </div>
            </>
          ) : (
            <div className="flex-1 min-w-0 flex flex-col">
              {jsonError && (
                <div className="mb-2 text-xs text-danger font-mono">{jsonError}</div>
              )}
              <textarea
                value={jsonText}
                onChange={(e) => onJsonChange(e.target.value)}
                spellCheck={false}
                className="flex-1 w-full resize-none rounded-2xl border border-border bg-bg p-4 font-mono text-[13px] leading-relaxed text-fg focus:outline-none focus:ring-2 focus:ring-brand/40"
              />
            </div>
          )}
        </div>

        {showChat && (
          <div className="w-[360px] shrink-0">
            <AuthoringChat name={name} onWorkflow={onAuthored} />
          </div>
        )}
      </div>

      {buildOutput && (
        <pre className="shrink-0 mt-3 max-h-40 overflow-y-auto rounded-xl border border-border bg-bg p-3 font-mono text-[11px] text-muted whitespace-pre-wrap">
          {buildOutput.stdout}
          {buildOutput.stderr}
        </pre>
      )}

      {pendingNewFrom && (
        <div
          className="fixed inset-0 z-50 grid place-items-center bg-black/40 backdrop-blur-sm p-4"
          onClick={() => setPendingNewFrom(null)}
        >
          <div
            className="w-full max-w-md rounded-2xl border border-border bg-surface p-5 shadow-xl"
            onClick={(e) => e.stopPropagation()}
          >
            <h2 className="text-base font-semibold text-fg">Create a new step?</h2>
            <p className="text-sm text-muted mt-2">
              Add a new step connected from{" "}
              <span className="font-mono text-fg">{pendingNewFrom}</span>.
            </p>
            <div className="mt-5 flex justify-end gap-2">
              <button
                onClick={() => setPendingNewFrom(null)}
                className="h-9 px-3 rounded-lg text-sm text-muted hover:text-fg hover:bg-elevated"
              >
                Cancel
              </button>
              <button
                onClick={createConnectedStep}
                className="h-9 px-3 rounded-lg text-sm font-medium bg-brand text-white hover:bg-brand-600"
              >
                Create step
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

function ModeToggle({ mode, onChange }: { mode: Mode; onChange: (m: Mode) => void }) {
  return (
    <div className="inline-flex rounded-lg border border-border bg-surface p-0.5">
      <ModeBtn active={mode === "flow"} onClick={() => onChange("flow")}>
        <WorkflowIcon className="w-4 h-4" /> Flow
      </ModeBtn>
      <ModeBtn active={mode === "json"} onClick={() => onChange("json")}>
        <CodeIcon className="w-4 h-4" /> JSON
      </ModeBtn>
    </div>
  );
}

function ModeBtn({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      onClick={onClick}
      className={cx(
        "inline-flex items-center gap-1.5 h-8 px-3 rounded-md text-sm font-medium",
        active ? "bg-brand/15 text-fg" : "text-muted hover:text-fg",
      )}
    >
      {children}
    </button>
  );
}
