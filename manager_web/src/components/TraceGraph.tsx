"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
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
  edgeHighlighted?: boolean;
};
type SlotNodeData = { label: string; value: unknown; edgeHighlighted?: boolean };

function StepNode({ data }: NodeProps<StepNodeData>) {
  return (
    <div
      className={[
        // Visited steps sit on the elevated surface; unvisited ("not run")
        // steps use a dashed muted border + faint fill so they stay clearly
        // readable against the canvas instead of fading into it.
        // FIXED width so the rendered size matches the size given to Dagre —
        // otherwise long labels grow the node past Dagre's estimate and nodes
        // collide.
        "rounded-xl px-3 py-2 w-[200px] shadow-sm transition-shadow",
        data.edgeHighlighted
          ? "border-2 bg-surface"
          : data.selected
            ? "border-2 border-brand ring-2 ring-brand/40 bg-surface"
            : data.visited
              ? "border border-border bg-surface"
              : "border border-dashed border-muted/60 bg-elevated/40",
      ].join(" ")}
      style={
        data.edgeHighlighted
          ? { borderColor: "rgb(96, 165, 250)", boxShadow: "0 0 0 3px rgba(96, 165, 250, 0.3)" }
          : undefined
      }
    >
      <Handle type="target" position={Position.Top} className="!bg-muted" />
      <div className="flex items-center gap-1.5">
        <span
          className={`text-[11px] uppercase tracking-wide ${
            data.visited ? "text-muted" : "text-muted/80"
          }`}
        >
          {data.kind === "start" ? "start" : "step"}
        </span>
        {data.visited ? (
          <span className="h-1.5 w-1.5 rounded-full bg-brand-500" title="entered" />
        ) : (
          <span className="text-[10px] text-muted">· not run</span>
        )}
      </div>
      <div
        className={`font-medium text-sm truncate ${
          data.visited ? "text-fg" : "text-muted"
        }`}
      >
        {data.label}
      </div>
      {data.durationMs != null && (
        <div className="text-[11px] text-muted mt-0.5">
          {fmtDuration(data.durationMs)}
        </div>
      )}
      <Handle type="source" position={Position.Bottom} className="!bg-muted" />
      <Handle id="slot" type="source" position={Position.Right} className="!bg-brand-500" />
    </div>
  );
}

function SlotNode({ data }: NodeProps<SlotNodeData>) {
  return (
    <div
      className={[
        "rounded-lg px-2.5 py-1.5 w-[160px] transition-shadow",
        data.edgeHighlighted
          ? "border-2 bg-brand/10"
          : "border border-brand/40 bg-brand/10",
      ].join(" ")}
      style={
        data.edgeHighlighted
          ? { borderColor: "rgb(96, 165, 250)", boxShadow: "0 0 0 3px rgba(96, 165, 250, 0.3)" }
          : undefined
      }
    >
      <Handle type="target" position={Position.Left} className="!bg-brand-500" />
      <div className="text-[10px] uppercase tracking-wide text-brand-700 dark:text-brand-300">
        memory
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
  const [onlyVisited, setOnlyVisited] = useState(false);
  const [showMemory, setShowMemory] = useState(false);
  const [selectedEdgeId, setSelectedEdgeId] = useState<string | null>(null);
  const slotCount = (doc.memory?.nodes ?? []).filter((n) => n.kind === "slot").length;

  const { nodes: baseNodes, edges, hasGraph } = useMemo(
    // "Only path taken" collapses to the steps that ran; "View memory" overlays
    // the slot (memory) nodes each step produced.
    () => buildGraph(doc, selectedStep, { showMemory, onlyVisited }),
    [doc, selectedStep, showMemory, onlyVisited],
  );

  // --- Controlled node state for drag support ------------------------------
  // `baseNodes` holds the Dagre-computed positions; `currentNodes` holds the
  // live positions that update when the user drags. Synced back whenever the
  // base graph changes (toggle, new data, etc.).
  const [currentNodes, setCurrentNodes] = useState<Node[]>(baseNodes);
  const [hasDragged, setHasDragged] = useState(false);

  useEffect(() => {
    setCurrentNodes(baseNodes);
    setHasDragged(false);
  }, [baseNodes]);

  const handleNodesChange = useCallback((changes: NodeChange[]) => {
    setCurrentNodes((nds) => applyNodeChanges(changes, nds));
    if (changes.some((c) => c.type === "position" && c.dragging)) {
      setHasDragged(true);
    }
  }, []);

  const handleResetLayout = useCallback(() => {
    setCurrentNodes(baseNodes);
    setHasDragged(false);
  }, [baseNodes]);

  // Derive display nodes/edges with edge-selection highlighting applied.
  // Kept separate from the layout memo so clicking an edge doesn't recompute
  // the Dagre layout.
  const EDGE_HL = "rgb(96, 165, 250)";
  const { displayNodes, displayEdges } = useMemo(() => {
    if (!selectedEdgeId) return { displayNodes: currentNodes, displayEdges: edges };
    const selEdge = edges.find((e) => e.id === selectedEdgeId);
    if (!selEdge) return { displayNodes: currentNodes, displayEdges: edges };

    const hlIds = new Set([selEdge.source, selEdge.target]);

    const displayNodes = currentNodes.map((n) =>
      hlIds.has(n.id)
        ? { ...n, data: { ...n.data, edgeHighlighted: true } }
        : n,
    );
    const displayEdges = edges.map((e) =>
      e.id === selectedEdgeId
        ? {
            ...e,
            animated: true,
            style: { ...e.style, stroke: EDGE_HL, strokeWidth: 2.5, opacity: 1 },
            markerEnd: {
              type: MarkerType.ArrowClosed,
              ...(typeof e.markerEnd === "object" ? e.markerEnd : {}),
              color: EDGE_HL,
            },
            labelStyle: { ...(e.labelStyle ?? {}), fill: EDGE_HL, fontWeight: 600 },
          }
        : e,
    );
    return { displayNodes, displayEdges };
  }, [currentNodes, edges, selectedEdgeId]);

  return (
    <div className="h-[600px] rounded-2xl border border-border bg-bg overflow-hidden relative">
      {/* controls */}
      <div className="absolute top-2 left-2 z-10 flex flex-wrap gap-1.5">
        {!hasGraph && (
          <span className="text-[11px] text-muted bg-surface/90 rounded px-2 py-1 border border-border">
            Older session — visited steps only.
          </span>
        )}
        {hasGraph && (
          <Toggle on={onlyVisited} onClick={() => setOnlyVisited((v) => !v)}>
            Only path taken
          </Toggle>
        )}
        {slotCount > 0 && (
          <Toggle on={showMemory} onClick={() => setShowMemory((v) => !v)}>
            View memory{showMemory ? "" : ` (${slotCount})`}
          </Toggle>
        )}
        {hasDragged && (
          <button
            onClick={handleResetLayout}
            className="text-[11px] rounded-md px-2 py-1 border bg-surface/90 border-border text-muted hover:text-fg flex items-center gap-1"
          >
            <span className="text-xs">&#x21bb;</span> Reset layout
          </button>
        )}
      </div>

      <ReactFlow
        nodes={displayNodes}
        edges={displayEdges}
        nodeTypes={nodeTypes}
        fitView
        fitViewOptions={{ padding: 0.2 }}
        minZoom={0.15}
        proOptions={{ hideAttribution: true }}
        onNodeClick={(_, node) => {
          setSelectedEdgeId(null);
          if (node.type === "step") onSelectStep(node.data.label);
        }}
        onEdgeClick={(_, edge) =>
          setSelectedEdgeId((prev) => (prev === edge.id ? null : edge.id))
        }
        onPaneClick={() => setSelectedEdgeId(null)}
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
            n.type === "slot"
              ? "rgb(166 221 31)"
              : (n.data as any)?.visited
                ? "rgb(130 176 21)"
                : "rgb(var(--border))"
          }
          nodeStrokeWidth={2}
        />
      </ReactFlow>
    </div>
  );
}

function Toggle({
  on,
  onClick,
  children,
}: {
  on: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      onClick={onClick}
      className={`text-[11px] rounded-md px-2 py-1 border ${
        on
          ? "bg-brand/15 border-brand/40 text-fg"
          : "bg-surface/90 border-border text-muted hover:text-fg"
      }`}
    >
      {children}
    </button>
  );
}

function buildGraph(
  doc: SessionDoc,
  selectedStep: string | null,
  opts: { showMemory: boolean; onlyVisited: boolean } = {
    showMemory: true,
    onlyVisited: false,
  },
): { nodes: Node[]; edges: Edge[]; hasGraph: boolean } {
  const graph = doc.workflow?.graph;
  const graphSteps = graph?.steps ?? {};
  const allStepIds = Object.keys(graphSteps);
  const hasGraph = allStepIds.length > 0;

  // --- trace-derived overlays ---------------------------------------------
  const visited = new Set<string>();
  for (const ev of doc.trace as TraceEvent[]) {
    if (ev.type === "step_enter" && ev.step) visited.add(ev.step);
  }

  // "Only path taken" collapses the graph to the steps that actually ran.
  const stepIds = opts.onlyVisited
    ? allStepIds.filter((id) => visited.has(id))
    : allStepIds;
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

  // --- step nodes (positions assigned by Dagre below) ---------------------
  const nodes: Node[] = stepIds.map((id) => {
    const s = graphSteps[id];
    return {
      id: `step:${id}`,
      type: "step",
      position: { x: 0, y: 0 },
      data: {
        label: id,
        kind: s?.type,
        durationMs: durByStep.get(id) ?? null,
        visited: visited.has(id),
        selected: selectedStep === id,
      },
    };
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
        type: "smoothstep",
        pathOptions: { borderRadius: 12 } as any,
        label,
        labelShowBg: true,
        animated: isTaken,
        markerEnd: {
          type: MarkerType.ArrowClosed,
          color: isTaken ? "rgb(166 221 31)" : "rgb(var(--muted))",
        },
        style: {
          // Inactive edges use the muted (zinc-500/400) color, not the faint
          // border color, so they stay readable against the canvas.
          stroke: isTaken ? "rgb(166 221 31)" : "rgb(var(--muted))",
          strokeWidth: isTaken ? 2.25 : 1.5,
          strokeDasharray: isDefault && (s?.choices?.length ?? 0) > 0 ? "5 4" : undefined,
          opacity: isTaken ? 1 : 0.85,
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

    (s?.choices ?? []).forEach((c) => {
      if (c.next) addEdge(c.next, condLabel(c.condition), false);
    });
    if (defaultNext) {
      addEdge(defaultNext, (s?.choices?.length ?? 0) > 0 ? "otherwise" : undefined, true);
    }
  }

  // --- memory slot nodes (added BEFORE layout so Dagre spaces them) --------
  // Each slot is a leaf attached to the step that produced it; Dagre ranks it
  // just below that step, guaranteeing no overlap regardless of count.
  if (opts.showMemory) {
    const producedBy = new Map<string, string>();
    for (const e of doc.memory?.edges ?? []) {
      if (e.from?.startsWith("step:") && e.to?.startsWith("slot:")) {
        producedBy.set(e.to, e.from.slice("step:".length));
      }
    }
    for (const n of doc.memory?.nodes ?? []) {
      if (n.kind !== "slot") continue;
      const stepName = producedBy.get(n.id);
      // Only attach slots whose producing step is in view.
      if (stepName && !graphSteps[stepName]) continue;
      nodes.push({
        id: n.id,
        type: "slot",
        position: { x: 0, y: 0 },
        data: { label: n.label ?? n.id, value: n.value },
      });
      if (stepName) {
        edges.push({
          id: `m:${stepName}->${n.id}`,
          source: `step:${stepName}`,
          sourceHandle: "slot",
          target: n.id,
          type: "smoothstep",
          style: { stroke: "rgb(166 221 31)", strokeWidth: 1.5, strokeDasharray: "4 3" },
        });
      }
    }
  }

  // --- layered layout via Dagre (handles ordering + crossing minimization) -
  layoutWithDagre(nodes, edges);

  return { nodes, edges, hasGraph: true };
}

// Must match the FIXED rendered node sizes (w-[200px] / w-[160px]) so Dagre's
// collision math is accurate. Heights are generous upper bounds (tallest
// variant: tag + label + duration + padding).
const STEP_W = 200;
const STEP_H = 80;
const SLOT_W = 160;
const SLOT_H = 60;

/** Position all nodes with Dagre (top-to-bottom layered DAG). Steps and slot
 *  nodes are sized by type; both step→step and step→slot edges participate so
 *  slots get their own non-overlapping positions. Mutates node positions. */
function layoutWithDagre(nodes: Node[], edges: Edge[]): void {
  const g = new dagre.graphlib.Graph();
  g.setGraph({
    rankdir: "TB",
    nodesep: 90, // horizontal gap between siblings — wide so labels/edges breathe
    ranksep: 130, // vertical gap between ranks — room for edge labels between rows
    edgesep: 30, // gap between parallel edges
    marginx: 30,
    marginy: 30,
  });
  g.setDefaultEdgeLabel(() => ({}));

  for (const n of nodes) {
    const isSlot = n.type === "slot";
    g.setNode(n.id, {
      width: isSlot ? SLOT_W : STEP_W,
      height: isSlot ? SLOT_H : STEP_H,
    });
  }
  // Every edge (step→step and step→slot) participates so nothing overlaps.
  for (const e of edges) {
    if (g.hasNode(e.source) && g.hasNode(e.target)) g.setEdge(e.source, e.target);
  }

  dagre.layout(g);

  for (const n of nodes) {
    const dn = g.node(n.id);
    if (!dn) continue;
    const w = n.type === "slot" ? SLOT_W : STEP_W;
    const h = n.type === "slot" ? SLOT_H : STEP_H;
    // Dagre returns node centers; ReactFlow wants top-left.
    n.position = { x: dn.x - w / 2, y: dn.y - h / 2 };
  }
}

function condLabel(cond: string): string {
  const c = (cond || "").trim();
  if (!c) return "if";
  return c.length > 28 ? c.slice(0, 28) + "…" : c;
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
  const nodes: Node[] = order.map((step) => ({
    id: `step:${step}`,
    type: "step",
    position: { x: 0, y: 0 },
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
      type: "smoothstep",
      markerEnd: { type: MarkerType.ArrowClosed, color: "rgb(166 221 31)" },
      style: { stroke: "rgb(166 221 31)", strokeWidth: 2 },
    });
  }
  layoutWithDagre(nodes, edges);
  return { nodes, edges, hasGraph: false };
}
