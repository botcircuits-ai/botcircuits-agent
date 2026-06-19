"use client";

import { useMemo } from "react";
import ReactFlow, {
  Background,
  Controls,
  Handle,
  MarkerType,
  Position,
  type Edge,
  type Node,
  type NodeProps,
} from "reactflow";
import "reactflow/dist/style.css";
import type { SessionDoc, TraceEvent } from "@/lib/api";
import { fmtDuration } from "@/lib/format";

/**
 * Workflow trace graph.
 *
 * The FULL branch topology comes from `workflow.graph` (every step and every
 * conditional edge, including paths this run did not take). The trace then
 * OVERLAYS reality: which steps were entered, per-step durations, and which
 * edge the engine actually followed at each branch. Slot nodes from the memory
 * graph attach to the step that produced them (the "memory flow").
 */

type StepNodeData = {
  label: string;
  kind?: string;
  durationMs: number | null;
  visited: boolean;
  selected: boolean;
};
type SlotNodeData = { label: string; value: unknown };

function StepNode({ data }: NodeProps<StepNodeData>) {
  const dim = !data.visited;
  return (
    <div
      className={[
        "rounded-xl border px-3 py-2 min-w-[160px] bg-surface shadow-sm",
        data.selected
          ? "border-brand ring-2 ring-brand/40"
          : data.visited
            ? "border-border"
            : "border-dashed border-border",
        dim ? "opacity-55" : "",
      ].join(" ")}
    >
      <Handle type="target" position={Position.Top} className="!bg-border" />
      <div className="flex items-center gap-1.5">
        <span className="text-[11px] uppercase tracking-wide text-muted">
          {data.kind === "start" ? "start" : "step"}
        </span>
        {data.visited ? (
          <span className="h-1.5 w-1.5 rounded-full bg-brand-500" title="entered" />
        ) : (
          <span className="text-[10px] text-muted">· not run</span>
        )}
      </div>
      <div className="font-medium text-fg text-sm truncate">{data.label}</div>
      {data.durationMs != null && (
        <div className="text-[11px] text-muted mt-0.5">
          {fmtDuration(data.durationMs)}
        </div>
      )}
      <Handle type="source" position={Position.Bottom} className="!bg-border" />
      <Handle id="slot" type="source" position={Position.Right} className="!bg-brand-500" />
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
  const { nodes, edges, hasGraph } = useMemo(
    () => buildGraph(doc, selectedStep),
    [doc, selectedStep],
  );

  return (
    <div className="h-[520px] rounded-2xl border border-border bg-bg overflow-hidden relative">
      {!hasGraph && (
        <div className="absolute top-2 left-2 z-10 text-[11px] text-muted bg-surface/80 rounded px-2 py-1 border border-border">
          Older session without graph data — showing visited steps only.
        </div>
      )}
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
): { nodes: Node[]; edges: Edge[]; hasGraph: boolean } {
  const graph = doc.workflow?.graph;
  const graphSteps = graph?.steps ?? {};
  const stepIds = Object.keys(graphSteps);
  const hasGraph = stepIds.length > 0;

  // --- trace-derived overlays ---------------------------------------------
  const visited = new Set<string>();
  for (const ev of doc.trace as TraceEvent[]) {
    if (ev.type === "step_enter" && ev.step) visited.add(ev.step);
  }
  // The engine's branch events tell us which edge was actually taken.
  const takenNextByStep = new Map<string, string | null>();
  for (const ev of doc.trace) {
    if (ev.type === "branch" && ev.step) {
      takenNextByStep.set(ev.step, ((ev.data as any)?.chosen_next ?? null) as string | null);
    }
  }
  // Per-step duration: attribute action_after durations to the most recent
  // step_enter (action events don't always carry a step id).
  const durByStep = new Map<string, number>();
  {
    let current: string | null = null;
    for (const ev of doc.trace) {
      if (ev.type === "step_enter" && ev.step) current = ev.step;
      if (ev.type === "action_after" && ev.duration_ms != null) {
        const k = ev.step ?? current;
        if (k) durByStep.set(k, (durByStep.get(k) ?? 0) + ev.duration_ms);
      }
    }
  }

  if (!hasGraph) {
    // Fallback for old sessions: linear visited-steps chain (previous behavior).
    return fallbackGraph(doc, selectedStep, durByStep);
  }

  // --- layout: BFS layering from the start step ---------------------------
  const startId = graph?.start && graphSteps[graph.start] ? graph.start : stepIds[0];
  const targetsOf = (id: string): string[] => {
    const s = graphSteps[id];
    const outs = new Set<string>();
    (s?.choices ?? []).forEach((c) => c.next && outs.add(c.next));
    if (s?.next) outs.add(s.next);
    return [...outs].filter((t) => graphSteps[t]); // only real steps
  };

  const depth = new Map<string, number>();
  const queue: string[] = [startId];
  depth.set(startId, 0);
  while (queue.length) {
    const id = queue.shift()!;
    for (const t of targetsOf(id)) {
      if (!depth.has(t)) {
        depth.set(t, (depth.get(id) ?? 0) + 1);
        queue.push(t);
      }
    }
  }
  // Steps unreachable from start still get placed at the end.
  stepIds.forEach((id) => {
    if (!depth.has(id)) depth.set(id, Math.max(0, ...depth.values()) + 1);
  });

  // Group by depth → rows; spread siblings across columns.
  const byDepth = new Map<number, string[]>();
  for (const id of stepIds) {
    const d = depth.get(id) ?? 0;
    (byDepth.get(d) ?? byDepth.set(d, []).get(d)!).push(id);
  }

  const nodes: Node[] = [];
  const colGap = 240;
  const rowGap = 130;
  const pos = new Map<string, { x: number; y: number }>();
  [...byDepth.keys()]
    .sort((a, b) => a - b)
    .forEach((d) => {
      const ids = byDepth.get(d)!;
      ids.forEach((id, i) => {
        const x = 60 + (i - (ids.length - 1) / 2) * colGap;
        const y = 30 + d * rowGap;
        pos.set(id, { x, y });
        const s = graphSteps[id];
        nodes.push({
          id: `step:${id}`,
          type: "step",
          position: { x, y },
          data: {
            label: id,
            kind: s?.type,
            durationMs: durByStep.get(id) ?? null,
            visited: visited.has(id),
            selected: selectedStep === id,
          },
        });
      });
    });

  // --- edges: every conditional + default, taken ones highlighted ---------
  const edges: Edge[] = [];
  for (const id of stepIds) {
    const s = graphSteps[id];
    const taken = takenNextByStep.get(id);
    const defaultNext = s?.next ?? null;

    const addEdge = (to: string, label: string | undefined, isDefault: boolean) => {
      if (!graphSteps[to]) return;
      const isTaken =
        (taken !== undefined && taken === to) ||
        // No branch event (non-branch step): the static default edge between
        // two visited steps counts as taken.
        (taken === undefined && isDefault && visited.has(id) && visited.has(to));
      edges.push({
        id: `e:${id}->${to}:${label ?? "next"}`,
        source: `step:${id}`,
        target: `step:${to}`,
        label,
        animated: isTaken,
        markerEnd: { type: MarkerType.ArrowClosed },
        style: {
          stroke: isTaken ? "rgb(166 221 31)" : "rgb(var(--border))",
          strokeWidth: isTaken ? 2 : 1.5,
          strokeDasharray: isDefault && (s?.choices?.length ?? 0) > 0 ? "5 4" : undefined,
        },
        labelStyle: { fill: "rgb(var(--muted))", fontSize: 11 },
        labelBgStyle: { fill: "rgb(var(--surface))", fillOpacity: 0.85 },
      });
    };

    (s?.choices ?? []).forEach((c) => {
      if (c.next) addEdge(c.next, condLabel(c.condition), false);
    });
    if (defaultNext) {
      addEdge(defaultNext, (s?.choices?.length ?? 0) > 0 ? "otherwise" : undefined, true);
    }
  }

  // --- memory slot nodes attach to producing step -------------------------
  attachMemory(doc, pos, nodes, edges);

  return { nodes, edges, hasGraph: true };
}

function condLabel(cond: string): string {
  const c = (cond || "").trim();
  if (!c) return "if";
  return c.length > 28 ? c.slice(0, 28) + "…" : c;
}

function attachMemory(
  doc: SessionDoc,
  pos: Map<string, { x: number; y: number }>,
  nodes: Node[],
  edges: Edge[],
) {
  const producedBy = new Map<string, string>();
  for (const e of doc.memory?.edges ?? []) {
    if (e.from?.startsWith("step:") && e.to?.startsWith("slot:")) {
      producedBy.set(e.to, e.from.slice("step:".length));
    }
  }
  let row = 0;
  for (const n of doc.memory?.nodes ?? []) {
    if (n.kind !== "slot") continue;
    const stepName = producedBy.get(n.id);
    const base = stepName ? pos.get(stepName) : undefined;
    const x = (base?.x ?? 60) + 260;
    const y = (base?.y ?? 30) + (row % 2) * 46;
    row++;
    nodes.push({
      id: n.id,
      type: "slot",
      position: { x, y },
      data: { label: n.label ?? n.id, value: n.value },
    });
    if (stepName) {
      edges.push({
        id: `m:${stepName}->${n.id}`,
        source: `step:${stepName}`,
        sourceHandle: "slot",
        target: n.id,
        style: { stroke: "rgb(166 221 31)", strokeDasharray: "4 3" },
      });
    }
  }
}

/** Legacy path for sessions captured before workflow.graph existed. */
function fallbackGraph(
  doc: SessionDoc,
  selectedStep: string | null,
  durByStep: Map<string, number>,
): { nodes: Node[]; edges: Edge[]; hasGraph: boolean } {
  const order: string[] = [];
  const seen = new Set<string>();
  for (const ev of doc.trace) {
    if (ev.type === "step_enter" && ev.step && !seen.has(ev.step)) {
      seen.add(ev.step);
      order.push(ev.step);
    }
  }
  const nodes: Node[] = order.map((step, i) => ({
    id: `step:${step}`,
    type: "step",
    position: { x: 80, y: 20 + i * 120 },
    data: {
      label: step,
      durationMs: durByStep.get(step) ?? null,
      visited: true,
      selected: selectedStep === step,
    },
  }));
  const edges: Edge[] = [];
  for (let i = 0; i < order.length - 1; i++) {
    edges.push({
      id: `e:${order[i]}->${order[i + 1]}`,
      source: `step:${order[i]}`,
      target: `step:${order[i + 1]}`,
      markerEnd: { type: MarkerType.ArrowClosed },
      style: { stroke: "rgb(166 221 31)", strokeWidth: 2 },
    });
  }
  attachMemory(doc, new Map(order.map((s, i) => [s, { x: 80, y: 20 + i * 120 }])), nodes, edges);
  return { nodes, edges, hasGraph: false };
}
