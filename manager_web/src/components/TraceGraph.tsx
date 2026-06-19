"use client";

import { useMemo } from "react";
import ReactFlow, {
  Background,
  Controls,
  Handle,
  Position,
  type Edge,
  type Node,
  type NodeProps,
} from "reactflow";
import "reactflow/dist/style.css";
import type { SessionDoc, TraceEvent } from "@/lib/api";
import { fmtDuration } from "@/lib/format";

/**
 * Trace + memory flow graph.
 *
 * Step nodes are derived from `step_enter` events in order; edges between
 * consecutive steps come from the engine's `branch` events (so branch labels
 * show which way it went). Slot nodes come from the session memory graph and
 * attach to the step that produced them — that's the "memory flow".
 */

type StepNodeData = {
  label: string;
  durationMs: number | null;
  branched: boolean | null;
  selected: boolean;
};
type SlotNodeData = { label: string; value: unknown };

function StepNode({ data }: NodeProps<StepNodeData>) {
  return (
    <div
      className={`rounded-xl border px-3 py-2 min-w-[150px] bg-surface shadow-sm ${
        data.selected ? "border-brand ring-2 ring-brand/40" : "border-border"
      }`}
    >
      <Handle type="target" position={Position.Top} className="!bg-border" />
      <div className="text-[11px] uppercase tracking-wide text-muted">step</div>
      <div className="font-medium text-fg text-sm truncate">{data.label}</div>
      {data.durationMs != null && (
        <div className="text-[11px] text-muted mt-0.5">
          {fmtDuration(data.durationMs)}
        </div>
      )}
      <Handle type="source" position={Position.Bottom} className="!bg-border" />
      <Handle
        id="slot"
        type="source"
        position={Position.Right}
        className="!bg-brand-500"
      />
    </div>
  );
}

function SlotNode({ data }: NodeProps<SlotNodeData>) {
  return (
    <div className="rounded-lg border border-brand/40 bg-brand/10 px-2.5 py-1.5 min-w-[120px]">
      <Handle type="target" position={Position.Left} className="!bg-brand-500" />
      <div className="text-[10px] uppercase tracking-wide text-brand-700 dark:text-brand-300">
        slot
      </div>
      <div className="font-mono text-xs text-fg truncate">{data.label}</div>
      <div className="font-mono text-[11px] text-muted truncate">
        {valuePreview(data.value)}
      </div>
    </div>
  );
}

function valuePreview(v: unknown): string {
  if (v === null || v === undefined) return "—";
  if (typeof v === "string") return v.length > 24 ? v.slice(0, 24) + "…" : v;
  try {
    return JSON.stringify(v);
  } catch {
    return String(v);
  }
}

const nodeTypes = { step: StepNode, slot: SlotNode };

export function TraceGraph({
  doc,
  selectedStep,
  onSelectStep,
}: {
  doc: SessionDoc;
  selectedStep: string | null;
  onSelectStep: (step: string | null) => void;
}) {
  const { nodes, edges } = useMemo(
    () => buildGraph(doc, selectedStep),
    [doc, selectedStep],
  );

  return (
    <div className="h-[520px] rounded-2xl border border-border bg-bg overflow-hidden">
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        fitView
        fitViewOptions={{ padding: 0.2 }}
        proOptions={{ hideAttribution: true }}
        onNodeClick={(_, node) => {
          if (node.type === "step") onSelectStep(node.data.label);
        }}
        nodesDraggable={false}
        nodesConnectable={false}
      >
        <Background gap={18} size={1} className="!text-border" color="currentColor" />
        <Controls showInteractive={false} />
      </ReactFlow>
    </div>
  );
}

function buildGraph(
  doc: SessionDoc,
  selectedStep: string | null,
): { nodes: Node[]; edges: Edge[] } {
  const nodes: Node[] = [];
  const edges: Edge[] = [];

  // Ordered, de-duplicated step sequence from step_enter events.
  const stepOrder: string[] = [];
  const seen = new Set<string>();
  for (const ev of doc.trace as TraceEvent[]) {
    if (ev.type === "step_enter" && ev.step && !seen.has(ev.step)) {
      seen.add(ev.step);
      stepOrder.push(ev.step);
    }
  }

  // Per-step duration: sum of action_after durations on that step.
  const durByStep = new Map<string, number>();
  for (const ev of doc.trace) {
    if (ev.type === "action_after" && ev.step && ev.duration_ms != null) {
      durByStep.set(ev.step, (durByStep.get(ev.step) ?? 0) + ev.duration_ms);
    }
  }
  // action_* events don't always carry a step id; fall back to attributing the
  // duration to the most recent step_enter before them.
  if (durByStep.size === 0) attributeDurations(doc.trace, stepOrder, durByStep);

  const x = 80;
  let y = 20;
  const rowGap = 120;
  const stepY = new Map<string, number>();

  stepOrder.forEach((step) => {
    stepY.set(step, y);
    nodes.push({
      id: `step:${step}`,
      type: "step",
      position: { x, y },
      data: {
        label: step,
        durationMs: durByStep.get(step) ?? null,
        branched: null,
        selected: selectedStep === step,
      },
    });
    y += rowGap;
  });

  // Sequential edges, labelled by branch decisions where present.
  const branchByStep = new Map<string, { chosen: string | null; branched: boolean }>();
  for (const ev of doc.trace) {
    if (ev.type === "branch" && ev.step) {
      const d = ev.data as any;
      branchByStep.set(ev.step, {
        chosen: d?.chosen_next ?? null,
        branched: !!d?.branched,
      });
    }
  }
  for (let i = 0; i < stepOrder.length - 1; i++) {
    const a = stepOrder[i];
    const b = stepOrder[i + 1];
    const br = branchByStep.get(a);
    edges.push({
      id: `e:${a}->${b}`,
      source: `step:${a}`,
      target: `step:${b}`,
      label: br?.branched ? `↳ ${br.chosen ?? b}` : undefined,
      animated: false,
      style: { stroke: "rgb(var(--border))" },
      labelStyle: { fill: "rgb(var(--muted))", fontSize: 11 },
    });
  }

  // Memory: slot nodes attached to the step that produced them.
  const slotX = x + 280;
  let slotRow = 0;
  const producedBy = new Map<string, string>();
  for (const e of doc.memory.edges) {
    if (e.from?.startsWith("step:") && e.to?.startsWith("slot:")) {
      producedBy.set(e.to, e.from.slice("step:".length));
    }
  }
  for (const n of doc.memory.nodes) {
    if (n.kind !== "slot") continue;
    const stepName = producedBy.get(n.id);
    const yPos =
      (stepName && stepY.get(stepName) != null
        ? (stepY.get(stepName) as number)
        : 20) +
      (slotRow % 2) * 44;
    slotRow++;
    nodes.push({
      id: n.id,
      type: "slot",
      position: { x: slotX, y: yPos },
      data: { label: n.label ?? n.id, value: n.value },
    });
    if (stepName) {
      edges.push({
        id: `m:${stepName}->${n.id}`,
        source: `step:${stepName}`,
        sourceHandle: "slot",
        target: n.id,
        style: { stroke: "rgb(var(--brand, 166 221 31))", strokeDasharray: "4 3" },
      });
    }
  }

  return { nodes, edges };
}

function attributeDurations(
  trace: TraceEvent[],
  stepOrder: string[],
  out: Map<string, number>,
) {
  let current: string | null = null;
  for (const ev of trace) {
    if (ev.type === "step_enter" && ev.step) current = ev.step;
    if (ev.type === "action_after" && ev.duration_ms != null && current) {
      out.set(current, (out.get(current) ?? 0) + ev.duration_ms);
    }
  }
}
