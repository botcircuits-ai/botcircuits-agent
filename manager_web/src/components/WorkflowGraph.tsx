"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import dagre from "@dagrejs/dagre";
import ReactFlow, {
  Background,
  ConnectionMode,
  Controls,
  Handle,
  MarkerType,
  MiniMap,
  Position,
  ReactFlowProvider,
  applyNodeChanges,
  type Edge,
  type Node,
  type NodeChange,
  type NodeProps,
  type OnConnectStartParams,
} from "reactflow";
import "reactflow/dist/style.css";
import type { WorkflowDoc, WorkflowStep } from "@/lib/api";

/**
 * UI flow editor canvas for a workflow source document.
 *
 * Beyond rendering, the canvas is directly editable:
 *   - inline-edit a step's name (double-click the title) and action (the body)
 *   - inline-edit an edge's condition (the edge label is an input)
 *   - drag from one node to another to create a connection (a branch condition,
 *     or the default `next` when the source has none yet)
 *   - drag from a node and drop on empty canvas to be asked whether to create a
 *     new connected step
 *
 * All mutations flow back through the typed callbacks so the parent `doc` stays
 * the single source of truth and the JSON view stays in sync.
 */

type EdgeKind = "default" | "condition";

type StepNodeData = {
  label: string;
  kind?: string;
  action?: string;
  selected: boolean;
  isStart: boolean;
  onSelect: (id: string) => void;
  onRename: (oldId: string, newId: string) => void;
  onAction: (id: string, action: string) => void;
};

function StepNode({ data, id }: NodeProps<StepNodeData>) {
  const stepId = id.replace(/^step:/, "");
  const [editingName, setEditingName] = useState(false);
  const [nameDraft, setNameDraft] = useState(data.label);
  const [actionDraft, setActionDraft] = useState(data.action ?? "");

  useEffect(() => setNameDraft(data.label), [data.label]);
  useEffect(() => setActionDraft(data.action ?? ""), [data.action]);

  const commitName = () => {
    setEditingName(false);
    const next = nameDraft.trim();
    if (next && next !== data.label) data.onRename(data.label, next);
    else setNameDraft(data.label);
  };

  return (
    <div
      onClick={() => data.onSelect(data.label)}
      className={[
        "rounded-xl px-3 py-2 w-[210px] shadow-sm transition-shadow border bg-surface",
        data.selected
          ? "border-2 border-brand ring-2 ring-brand/40"
          : "border-border",
      ].join(" ")}
    >
      {/* With connectionMode="loose" a single handle acts as both source and
          target, so a connection can be drawn from either node's handle. */}
      <Handle type="target" position={Position.Top} className="!bg-muted !w-3 !h-3" />
      <div className="flex items-center gap-1.5">
        <span className="text-[11px] uppercase tracking-wide text-muted">
          {data.isStart ? "start" : data.kind || "step"}
        </span>
      </div>

      {editingName ? (
        <input
          autoFocus
          value={nameDraft}
          onChange={(e) => setNameDraft(e.target.value)}
          onBlur={commitName}
          onKeyDown={(e) => {
            if (e.key === "Enter") commitName();
            if (e.key === "Escape") {
              setNameDraft(data.label);
              setEditingName(false);
            }
          }}
          className="nodrag w-full rounded border border-brand/50 bg-bg px-1 py-0.5 text-sm font-medium font-mono text-fg focus:outline-none"
        />
      ) : (
        <div
          className="font-medium text-sm truncate text-fg cursor-text"
          title="Double-click to rename"
          onDoubleClick={(e) => {
            e.stopPropagation();
            if (!data.isStart) setEditingName(true);
          }}
        >
          {data.label}
        </div>
      )}

      {!data.isStart && (
        <textarea
          value={actionDraft}
          onChange={(e) => setActionDraft(e.target.value)}
          onBlur={() => {
            if (actionDraft !== (data.action ?? "")) data.onAction(stepId, actionDraft);
          }}
          onClick={(e) => e.stopPropagation()}
          rows={2}
          placeholder="action prompt…"
          className="nodrag nowheel mt-1 w-full resize-none rounded border border-border bg-bg px-1.5 py-1 text-[11px] leading-snug text-fg placeholder:text-muted focus:outline-none focus:ring-1 focus:ring-brand/40"
        />
      )}

      <Handle type="source" position={Position.Bottom} className="!bg-brand-500 !w-3 !h-3" />
    </div>
  );
}

const nodeTypes = { step: StepNode };

export type WorkflowGraphHandlers = {
  onSelectStep: (step: string | null) => void;
  onRenameStep: (oldId: string, newId: string) => void;
  onUpdateAction: (id: string, action: string) => void;
  onUpdateEdgeCondition: (
    from: string,
    kind: EdgeKind,
    condIndex: number,
    condition: string,
  ) => void;
  /** Connect two existing steps. */
  onConnect: (from: string, to: string) => void;
  /** Drag ended on empty canvas: caller asks the user, then maybe creates. */
  onConnectToEmpty: (from: string) => void;
};

export function WorkflowGraph(props: {
  doc: WorkflowDoc;
  selectedStep: string | null;
} & WorkflowGraphHandlers) {
  return (
    <ReactFlowProvider>
      <WorkflowGraphInner {...props} />
    </ReactFlowProvider>
  );
}

function WorkflowGraphInner({
  doc,
  selectedStep,
  onSelectStep,
  onRenameStep,
  onUpdateAction,
  onUpdateEdgeCondition,
  onConnect,
  onConnectToEmpty,
}: {
  doc: WorkflowDoc;
  selectedStep: string | null;
} & WorkflowGraphHandlers) {
  const { nodes: baseNodes, edges } = useMemo(
    () =>
      buildGraph(doc, selectedStep, {
        onSelect: onSelectStep,
        onRename: onRenameStep,
        onAction: onUpdateAction,
        onUpdateEdgeCondition,
      }),
    [doc, selectedStep, onSelectStep, onRenameStep, onUpdateAction, onUpdateEdgeCondition],
  );

  const [currentNodes, setCurrentNodes] = useState<Node[]>(baseNodes);
  useEffect(() => setCurrentNodes(baseNodes), [baseNodes]);

  const handleNodesChange = (changes: NodeChange[]) =>
    setCurrentNodes((nds) => applyNodeChanges(changes, nds));

  // Track where a connection drag started so a drop on empty canvas can create
  // a new step connected from that source.
  const connectFrom = useRef<string | null>(null);
  const onConnectStart = useCallback(
    (_: unknown, params: OnConnectStartParams) => {
      connectFrom.current = params.nodeId ? params.nodeId.replace(/^step:/, "") : null;
    },
    [],
  );

  // Connect by dropping anywhere over the target node's surface — not just on
  // its top handle. We resolve the node under the pointer at drop time and, if
  // found, connect to it; otherwise (empty canvas) raise the create dialog.
  const onConnectEnd = useCallback(
    (event: MouseEvent | TouchEvent) => {
      const from = connectFrom.current;
      connectFrom.current = null;
      if (!from) return;

      const point =
        "changedTouches" in event && event.changedTouches.length
          ? event.changedTouches[0]
          : (event as MouseEvent);
      const el = document.elementFromPoint(point.clientX, point.clientY) as
        | HTMLElement
        | null;
      const nodeEl = el?.closest(".react-flow__node") as HTMLElement | null;
      const toId = nodeEl?.getAttribute("data-id")?.replace(/^step:/, "") ?? null;

      if (toId && toId !== from) {
        onConnect(from, toId);
      } else if (!toId) {
        onConnectToEmpty(from);
      }
    },
    [onConnect, onConnectToEmpty],
  );

  return (
    <div className="h-full w-full rounded-2xl border border-border bg-bg overflow-hidden">
      <ReactFlow
        nodes={currentNodes}
        edges={edges}
        nodeTypes={nodeTypes}
        connectionMode={ConnectionMode.Loose}
        fitView
        fitViewOptions={{ padding: 0.2 }}
        minZoom={0.15}
        proOptions={{ hideAttribution: true }}
        onPaneClick={() => onSelectStep(null)}
        onNodesChange={handleNodesChange}
        onConnectStart={onConnectStart}
        onConnectEnd={onConnectEnd}
        nodesDraggable
        nodesConnectable
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
  cb: {
    onSelect: (id: string) => void;
    onRename: (oldId: string, newId: string) => void;
    onAction: (id: string, action: string) => void;
    onUpdateEdgeCondition: (
      from: string,
      kind: EdgeKind,
      condIndex: number,
      condition: string,
    ) => void;
  },
): { nodes: Node[]; edges: Edge[] } {
  const steps = doc.flow?.steps ?? {};
  const start = doc.flow?.start ?? "start";
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
        onSelect: cb.onSelect,
        onRename: cb.onRename,
        onAction: cb.onAction,
      } satisfies StepNodeData,
    };
  });

  const edges: Edge[] = [];
  for (const id of ids) {
    const s = steps[id] ?? {};
    const branching = (s.conditions?.length ?? 0) > 0;

    (s.conditions ?? []).forEach((c, idx) => {
      if (!c.next || !steps[c.next]) return;
      edges.push(
        makeEdge(id, c.next, c.condition, "condition", idx, cb.onUpdateEdgeCondition),
      );
    });
    if (s.next && steps[s.next]) {
      edges.push(
        makeEdge(
          id,
          s.next,
          branching ? "otherwise" : "",
          "default",
          -1,
          cb.onUpdateEdgeCondition,
          /*readOnlyLabel*/ true,
        ),
      );
    }
  }

  layoutWithDagre(nodes, edges);
  return { nodes, edges };
}

function makeEdge(
  from: string,
  to: string,
  condition: string,
  kind: EdgeKind,
  condIndex: number,
  onUpdate: (from: string, kind: EdgeKind, condIndex: number, condition: string) => void,
  readOnlyLabel = false,
): Edge {
  return {
    id: `e:${from}->${to}:${kind}:${condIndex}`,
    source: `step:${from}`,
    target: `step:${to}`,
    type: "smoothstep",
    pathOptions: { borderRadius: 12 } as any,
    label: readOnlyLabel ? (condition || undefined) : (
      <EdgeConditionInput
        value={condition}
        onCommit={(v) => onUpdate(from, kind, condIndex, v)}
      />
    ),
    markerEnd: { type: MarkerType.ArrowClosed, color: "rgb(var(--muted))" },
    style: {
      stroke: "rgb(var(--muted))",
      strokeWidth: 1.6,
      strokeDasharray: kind === "default" && readOnlyLabel ? "5 4" : undefined,
      opacity: 0.9,
    },
    labelBgStyle: { fill: "transparent" },
    labelStyle: { fill: "rgb(var(--fg))", fontSize: 10, fontWeight: 500 },
  };
}

/** Inline-editable condition label rendered on a branch edge. */
function EdgeConditionInput({
  value,
  onCommit,
}: {
  value: string;
  onCommit: (v: string) => void;
}) {
  const [draft, setDraft] = useState(value);
  useEffect(() => setDraft(value), [value]);
  return (
    <input
      value={draft}
      onChange={(e) => setDraft(e.target.value)}
      onBlur={() => {
        if (draft !== value) onCommit(draft);
      }}
      onKeyDown={(e) => {
        if (e.key === "Enter") (e.target as HTMLInputElement).blur();
      }}
      placeholder="condition…"
      className="nodrag nowheel rounded border border-border bg-elevated px-1.5 py-0.5 text-[10px] text-fg placeholder:text-muted focus:outline-none focus:ring-1 focus:ring-brand/40"
      style={{ width: Math.max(60, Math.min(180, (draft.length || 8) * 6)) }}
    />
  );
}

const STEP_W = 210;
const STEP_H = 110;

function layoutWithDagre(nodes: Node[], edges: Edge[]): void {
  const g = new dagre.graphlib.Graph();
  g.setGraph({
    rankdir: "TB",
    nodesep: 90,
    ranksep: 130,
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
