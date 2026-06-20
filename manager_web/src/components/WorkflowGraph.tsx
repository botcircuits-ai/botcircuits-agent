"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import dagre from "@dagrejs/dagre";
import ReactFlow, {
  BaseEdge,
  Background,
  ConnectionMode,
  Controls,
  EdgeLabelRenderer,
  Handle,
  MarkerType,
  MiniMap,
  Position,
  ReactFlowProvider,
  applyNodeChanges,
  getSmoothStepPath,
  type Edge,
  type EdgeProps,
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
        edgeTypes={edgeTypes}
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

    (s.conditions ?? []).forEach((c, idx) => {
      if (!c.next || !steps[c.next]) return;
      edges.push(
        makeEdge(id, c.next, c.condition, "condition", idx, cb.onUpdateEdgeCondition),
      );
    });
    if (s.next && steps[s.next]) {
      // The default ("otherwise") edge is also editable: typing a condition
      // into it converts it from the default path into a regular branch (the
      // update handler moves `next` into `conditions` and clears `next`).
      edges.push(
        makeEdge(id, s.next, "", "default", -1, cb.onUpdateEdgeCondition),
      );
    }
  }

  layoutWithDagre(nodes, edges);
  return { nodes, edges };
}

type ConditionEdgeData = {
  from: string;
  kind: EdgeKind;
  condIndex: number;
  condition: string;
  onUpdate: (from: string, kind: EdgeKind, condIndex: number, condition: string) => void;
};

function makeEdge(
  from: string,
  to: string,
  condition: string,
  kind: EdgeKind,
  condIndex: number,
  onUpdate: (from: string, kind: EdgeKind, condIndex: number, condition: string) => void,
): Edge<ConditionEdgeData> {
  const isDefault = kind === "default";
  return {
    id: `e:${from}->${to}:${kind}:${condIndex}`,
    source: `step:${from}`,
    target: `step:${to}`,
    type: "condition",
    markerEnd: { type: MarkerType.ArrowClosed, color: "rgb(var(--muted))" },
    style: {
      stroke: "rgb(var(--muted))",
      strokeWidth: 1.6,
      strokeDasharray: isDefault ? "5 4" : undefined,
      opacity: 0.9,
    },
    data: { from, kind, condIndex, condition, onUpdate },
  };
}

/** Custom edge: smoothstep path with an interactive HTML label at the midpoint.
 *  Uses EdgeLabelRenderer so the label is real, clickable HTML (the `label`
 *  prop only renders static, non-interactive text). */
function ConditionEdge({
  id,
  sourceX,
  sourceY,
  targetX,
  targetY,
  sourcePosition,
  targetPosition,
  markerEnd,
  style,
  data,
}: EdgeProps<ConditionEdgeData>) {
  const [path, labelX, labelY] = getSmoothStepPath({
    sourceX,
    sourceY,
    targetX,
    targetY,
    sourcePosition,
    targetPosition,
    borderRadius: 12,
  });

  return (
    <>
      <BaseEdge id={id} path={path} markerEnd={markerEnd} style={style} />
      {data && (
        <EdgeLabelRenderer>
          <div
            className="nodrag nopan"
            style={{
              position: "absolute",
              transform: `translate(-50%, -50%) translate(${labelX}px, ${labelY}px)`,
              pointerEvents: "all",
            }}
          >
            <EdgeConditionInput
              value={data.condition}
              isDefault={data.kind === "default"}
              onCommit={(v) => data.onUpdate(data.from, data.kind, data.condIndex, v)}
            />
          </div>
        </EdgeLabelRenderer>
      )}
    </>
  );
}

const edgeTypes = { condition: ConditionEdge };

/**
 * Inline condition label for a branch edge.
 *
 * When the condition is empty it shows a compact "Add condition" pill; clicking
 * it (or an existing condition's text) opens an inline input to edit the
 * condition prompt. Commits on blur / Enter, cancels on Escape.
 */
function EdgeConditionInput({
  value,
  isDefault = false,
  onCommit,
}: {
  value: string;
  isDefault?: boolean;
  onCommit: (v: string) => void;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(value);
  useEffect(() => setDraft(value), [value]);

  if (!editing) {
    const empty = !value.trim();
    // The default edge with no condition reads as "(default)"; typing
    // a condition into it turns it into a regular branch.
    const emptyLabel = isDefault ? "default" : "+ Add condition";
    return (
      <button
        onClick={() => setEditing(true)}
        className={[
          "nodrag rounded px-1.5 py-0.5 text-[10px] font-medium border",
          empty
            ? isDefault
              ? "border-dashed border-border text-muted bg-elevated hover:bg-surface"
              : "border-dashed border-brand/50 text-brand-600 dark:text-brand-400 bg-brand/10 hover:bg-brand/20"
            : "border-border bg-elevated text-fg hover:bg-surface",
        ].join(" ")}
        title={empty ? "Click to set a condition" : "Edit condition"}
      >
        {empty ? emptyLabel : value}
      </button>
    );
  }

  const commit = () => {
    setEditing(false);
    if (draft !== value) onCommit(draft);
  };

  return (
    <input
      autoFocus
      value={draft}
      onChange={(e) => setDraft(e.target.value)}
      onBlur={commit}
      onKeyDown={(e) => {
        if (e.key === "Enter") commit();
        if (e.key === "Escape") {
          setDraft(value);
          setEditing(false);
        }
      }}
      placeholder="condition…"
      className="nodrag nowheel rounded border border-brand/50 bg-elevated px-1.5 py-0.5 text-[10px] text-fg placeholder:text-muted focus:outline-none focus:ring-1 focus:ring-brand/40"
      style={{ width: Math.max(80, Math.min(180, (draft.length || 10) * 6)) }}
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
