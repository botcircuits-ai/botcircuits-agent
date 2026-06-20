"use client";

import { useEffect, useMemo, useState } from "react";
import dagre from "@dagrejs/dagre";
import ReactFlow, {
  Background,
  Controls,
  Handle,
  MarkerType,
  MiniMap,
  Position,
  applyNodeChanges,
  type Edge,
  type Node,
  type NodeChange,
  type NodeProps,
} from "reactflow";
import "reactflow/dist/style.css";
import type { WorkflowDoc, WorkflowStep } from "@/lib/api";

/**
 * UI flow editor canvas for a workflow source document.
 *
 * Renders every step in `flow.steps` and every edge (default `next` plus each
 * conditional `next`). Clicking a step selects it for editing in the side
 * panel. The graph is a VIEW over the `doc` prop — all mutations flow back
 * through `onSelectStep`; structural edits happen in the side panel so the
 * single source of truth stays the parent's `doc`.
 */

type StepNodeData = {
  label: string;
  kind?: string;
  action?: string;
  selected: boolean;
  isStart: boolean;
};

function StepNode({ data }: NodeProps<StepNodeData>) {
  return (
    <div
      className={[
        "rounded-xl px-3 py-2 w-[210px] shadow-sm transition-shadow border bg-surface",
        data.selected
          ? "border-2 border-brand ring-2 ring-brand/40"
          : "border-border",
      ].join(" ")}
    >
      <Handle type="target" position={Position.Top} className="!bg-muted" />
      <div className="flex items-center gap-1.5">
        <span className="text-[11px] uppercase tracking-wide text-muted">
          {data.isStart ? "start" : data.kind || "step"}
        </span>
      </div>
      <div className="font-medium text-sm truncate text-fg">{data.label}</div>
      {data.action && (
        <div className="text-[11px] text-muted mt-0.5 line-clamp-2">
          {data.action}
        </div>
      )}
      <Handle type="source" position={Position.Bottom} className="!bg-muted" />
    </div>
  );
}

const nodeTypes = { step: StepNode };

export function WorkflowGraph({
  doc,
  selectedStep,
  onSelectStep,
}: {
  doc: WorkflowDoc;
  selectedStep: string | null;
  onSelectStep: (step: string | null) => void;
}) {
  const { nodes: baseNodes, edges } = useMemo(
    () => buildGraph(doc, selectedStep),
    [doc, selectedStep],
  );

  const [currentNodes, setCurrentNodes] = useState<Node[]>(baseNodes);
  useEffect(() => setCurrentNodes(baseNodes), [baseNodes]);

  const handleNodesChange = (changes: NodeChange[]) =>
    setCurrentNodes((nds) => applyNodeChanges(changes, nds));

  return (
    <div className="h-full w-full rounded-2xl border border-border bg-bg overflow-hidden">
      <ReactFlow
        nodes={currentNodes}
        edges={edges}
        nodeTypes={nodeTypes}
        fitView
        fitViewOptions={{ padding: 0.2 }}
        minZoom={0.15}
        proOptions={{ hideAttribution: true }}
        onNodeClick={(_, node) => onSelectStep(node.data.label)}
        onPaneClick={() => onSelectStep(null)}
        onNodesChange={handleNodesChange}
        nodesDraggable
        nodesConnectable={false}
      >
        <Background gap={18} size={1} className="!text-border" color="currentColor" />
        <Controls showInteractive={false} />
        <MiniMap
          pannable
          zoomable
          className="!bg-surface !border !border-border rounded-lg"
          maskColor="rgb(var(--bg) / 0.6)"
          nodeColor={(n) =>
            (n.data as any)?.selected ? "rgb(130 176 21)" : "rgb(var(--border))"
          }
          nodeStrokeWidth={2}
        />
      </ReactFlow>
    </div>
  );
}

function buildGraph(
  doc: WorkflowDoc,
  selectedStep: string | null,
): { nodes: Node[]; edges: Edge[] } {
  const steps = doc.flow?.steps ?? {};
  const start = doc.flow?.start ?? null;
  const ids = Object.keys(steps);

  const nodes: Node[] = ids.map((id) => {
    const s: WorkflowStep = steps[id] ?? {};
    return {
      id: `step:${id}`,
      type: "step",
      position: { x: 0, y: 0 },
      data: {
        label: id,
        kind: s.type,
        action: s.settings?.action,
        selected: selectedStep === id,
        isStart: id === start || s.type === "start",
      },
    };
  });

  const edges: Edge[] = [];
  const addEdge = (from: string, to: string, label: string | undefined, isDefault: boolean) => {
    if (!steps[to]) return;
    edges.push({
      id: `e:${from}->${to}:${label ?? "next"}`,
      source: `step:${from}`,
      target: `step:${to}`,
      type: "smoothstep",
      pathOptions: { borderRadius: 12 } as any,
      label,
      labelShowBg: true,
      markerEnd: { type: MarkerType.ArrowClosed, color: "rgb(var(--muted))" },
      style: {
        stroke: "rgb(var(--muted))",
        strokeWidth: 1.6,
        strokeDasharray: isDefault && hasConditions(steps[from]) ? "5 4" : undefined,
        opacity: 0.9,
      },
      labelStyle: { fill: "rgb(var(--fg))", fontSize: 10, fontWeight: 500 },
      labelBgStyle: {
        fill: "rgb(var(--elevated))",
        fillOpacity: 1,
        stroke: "rgb(var(--border))",
      },
      labelBgPadding: [5, 3],
      labelBgBorderRadius: 4,
    });
  };

  for (const id of ids) {
    const s = steps[id] ?? {};
    (s.conditions ?? []).forEach((c) => {
      if (c.next) addEdge(id, c.next, condLabel(c.condition), false);
    });
    if (s.next) addEdge(id, s.next, hasConditions(s) ? "otherwise" : undefined, true);
  }

  layoutWithDagre(nodes, edges);
  return { nodes, edges };
}

function hasConditions(s: WorkflowStep | undefined): boolean {
  return (s?.conditions?.length ?? 0) > 0;
}

const STEP_W = 210;
const STEP_H = 88;

function layoutWithDagre(nodes: Node[], edges: Edge[]): void {
  const g = new dagre.graphlib.Graph();
  g.setGraph({
    rankdir: "TB",
    nodesep: 90,
    ranksep: 120,
    edgesep: 30,
    marginx: 30,
    marginy: 30,
  });
  g.setDefaultEdgeLabel(() => ({}));
  for (const n of nodes) g.setNode(n.id, { width: STEP_W, height: STEP_H });
  for (const e of edges) {
    if (g.hasNode(e.source) && g.hasNode(e.target)) g.setEdge(e.source, e.target);
  }
  dagre.layout(g);
  for (const n of nodes) {
    const dn = g.node(n.id);
    if (!dn) continue;
    n.position = { x: dn.x - STEP_W / 2, y: dn.y - STEP_H / 2 };
  }
}

function condLabel(cond: string): string {
  const c = (cond || "").trim();
  if (!c) return "if";
  return c.length > 28 ? c.slice(0, 28) + "…" : c;
}
