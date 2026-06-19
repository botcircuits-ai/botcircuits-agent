"use client";

import { useState } from "react";
import type { TraceEvent } from "@/lib/api";
import { cx, eventDotColor, eventLabel, fmtDuration } from "@/lib/format";

/** Vertical event timeline. Each event expands to show its slot snapshot and
 *  type-specific data (action I/O, branch decision, resolved slots). */
export function TraceTimeline({
  events,
  highlightStep,
}: {
  events: TraceEvent[];
  highlightStep: string | null;
}) {
  return (
    <ol className="relative">
      {events.map((ev, i) => (
        <TimelineRow
          key={ev.seq}
          ev={ev}
          last={i === events.length - 1}
          highlight={!!highlightStep && ev.step === highlightStep}
        />
      ))}
    </ol>
  );
}

function TimelineRow({
  ev,
  last,
  highlight,
}: {
  ev: TraceEvent;
  last: boolean;
  highlight: boolean;
}) {
  const [open, setOpen] = useState(false);
  const hasDetail =
    Object.keys(ev.data ?? {}).length > 0 ||
    Object.keys(ev.slots ?? {}).length > 0;

  return (
    <li className="relative pl-8 pb-4">
      {!last && (
        <span className="absolute left-[7px] top-4 bottom-0 w-px bg-border" />
      )}
      <span
        className={cx(
          "absolute left-0 top-1.5 h-3.5 w-3.5 rounded-full ring-4 ring-bg",
          eventDotColor(ev.type),
        )}
      />
      <div
        className={cx(
          "rounded-xl border px-3 py-2",
          highlight ? "border-brand/50 bg-brand/5" : "border-border bg-surface",
        )}
      >
        <button
          onClick={() => hasDetail && setOpen((o) => !o)}
          className="w-full flex items-center gap-2 text-left"
        >
          <span className="text-sm font-medium text-fg">
            {eventLabel(ev.type)}
          </span>
          {ev.step && (
            <span className="font-mono text-xs text-muted">{ev.step}</span>
          )}
          {ev.duration_ms != null && (
            <span className="text-xs text-brand-700 dark:text-brand-300">
              {fmtDuration(ev.duration_ms)}
            </span>
          )}
          <span className="ml-auto text-xs text-muted tabular-nums">
            #{ev.seq}
          </span>
          {hasDetail && (
            <span className="text-muted text-xs">{open ? "▾" : "▸"}</span>
          )}
        </button>

        {open && (
          <div className="mt-2 space-y-2">
            <EventData ev={ev} />
            {Object.keys(ev.slots ?? {}).length > 0 && (
              <Section title="Memory at this point">
                <KeyVals obj={ev.slots} />
              </Section>
            )}
          </div>
        )}
      </div>
    </li>
  );
}

function EventData({ ev }: { ev: TraceEvent }) {
  const d = ev.data ?? {};
  if (ev.type === "action_after") {
    const out = (d as any).output ?? {};
    return (
      <>
        {(d as any).input?.actions && (
          <Section title="Action (input)">
            <ul className="list-disc pl-4 text-sm text-fg space-y-0.5">
              {((d as any).input.actions as string[]).map((a, i) => (
                <li key={i}>{a}</li>
              ))}
            </ul>
          </Section>
        )}
        <Section title="Sub-agent output">
          {out.text ? (
            <p className="text-sm text-fg whitespace-pre-wrap">{out.text}</p>
          ) : (
            <p className="text-sm text-muted">—</p>
          )}
          {out.captured_slots &&
            Object.keys(out.captured_slots).length > 0 && (
              <div className="mt-1">
                <KeyVals obj={out.captured_slots} />
              </div>
            )}
        </Section>
      </>
    );
  }
  if (ev.type === "branch") {
    return (
      <Section title="Branch decision">
        <div className="text-sm text-fg">
          → <code className="font-mono">{(d as any).chosen_next ?? "end"}</code>
          {(d as any).branched ? (
            <span className="ml-2 text-xs text-warn">(branched)</span>
          ) : (
            <span className="ml-2 text-xs text-muted">(default)</span>
          )}
        </div>
      </Section>
    );
  }
  if (ev.type === "slot_resolve") {
    return (
      <Section title="Resolved memory">
        <KeyVals obj={(d as any).resolved ?? {}} />
      </Section>
    );
  }
  if (Object.keys(d).length === 0) return null;
  return (
    <Section title="Data">
      <KeyVals obj={d} />
    </Section>
  );
}

function Section({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}) {
  return (
    <div className="rounded-lg bg-elevated/60 border border-border px-2.5 py-2">
      <div className="text-[11px] uppercase tracking-wide text-muted mb-1">
        {title}
      </div>
      {children}
    </div>
  );
}

function KeyVals({ obj }: { obj: Record<string, unknown> }) {
  const entries = Object.entries(obj);
  if (entries.length === 0)
    return <span className="text-sm text-muted">—</span>;
  return (
    <div className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5">
      {entries.map(([k, v]) => (
        <div key={k} className="contents">
          <span className="font-mono text-xs text-muted">{k}</span>
          <span className="font-mono text-xs text-fg break-all">
            {typeof v === "string" ? v : JSON.stringify(v)}
          </span>
        </div>
      ))}
    </div>
  );
}
