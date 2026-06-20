"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { SparkleIcon } from "@/components/icons";
import { api, type WorkflowDoc } from "@/lib/api";
import { useAuth } from "@/lib/auth";

/**
 * Natural-language authoring chat.
 *
 * Sends an instruction to the backend's SSE authoring endpoint, which drives
 * the configured agent runtime (default claude-code) to write + build the
 * workflow. Streams the runtime's log lines live; on `done`, hands the new
 * workflow source back to the editor via `onWorkflow` so both editor modes
 * sync to the generated result.
 */

type ChatMsg =
  | { role: "user"; text: string }
  | { role: "assistant"; text: string; done?: boolean; ok?: boolean };

export function AuthoringChat({
  name,
  onWorkflow,
}: {
  name: string;
  onWorkflow: (doc: WorkflowDoc) => void;
}) {
  const { token } = useAuth();
  const [input, setInput] = useState("");
  const [running, setRunning] = useState(false);
  const [messages, setMessages] = useState<ChatMsg[]>([]);
  const esRef = useRef<EventSource | null>(null);
  const scrollRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight });
  }, [messages]);

  useEffect(() => () => esRef.current?.close(), []);

  const send = useCallback(() => {
    const instruction = input.trim();
    if (!instruction || !token || running) return;
    if (!name) return;

    setInput("");
    setMessages((m) => [
      ...m,
      { role: "user", text: instruction },
      { role: "assistant", text: "" },
    ]);
    setRunning(true);

    const appendLog = (line: string) =>
      setMessages((m) => {
        const last = m[m.length - 1];
        if (last?.role !== "assistant") return m;
        const text = last.text ? last.text + "\n" + line : line;
        return [...m.slice(0, -1), { ...last, text }];
      });

    const es = new EventSource(api.authorStreamUrl(token, name, instruction));
    esRef.current = es;

    es.addEventListener("start", (e) => {
      const d = JSON.parse((e as MessageEvent).data);
      appendLog(`▸ running ${d.runtime}…`);
    });
    es.addEventListener("log", (e) => {
      const d = JSON.parse((e as MessageEvent).data);
      appendLog(d.line);
    });
    es.addEventListener("error", (e) => {
      const data = (e as MessageEvent).data;
      if (data) {
        try {
          appendLog("⚠ " + JSON.parse(data).message);
        } catch {
          appendLog("⚠ stream error");
        }
      } else {
        appendLog("⚠ connection lost");
      }
      setRunning(false);
      es.close();
    });
    es.addEventListener("done", (e) => {
      const d = JSON.parse((e as MessageEvent).data);
      setMessages((m) => {
        const last = m[m.length - 1];
        if (last?.role !== "assistant") return m;
        const text =
          last.text +
          "\n" +
          (d.ok ? "✓ workflow updated." : "⚠ finished, but no workflow file found.");
        return [...m.slice(0, -1), { ...last, text, done: true, ok: d.ok }];
      });
      if (d.ok && d.workflow) onWorkflow(d.workflow as WorkflowDoc);
      setRunning(false);
      es.close();
    });
  }, [input, token, running, name, onWorkflow]);

  return (
    <div className="flex flex-col h-full border-l border-border bg-surface">
      <div className="h-12 shrink-0 flex items-center gap-2 px-4 border-b border-border">
        <SparkleIcon className="w-[18px] h-[18px] text-brand-600 dark:text-brand-400" />
        <span className="text-sm font-medium text-fg">Author with AI</span>
      </div>

      <div ref={scrollRef} className="flex-1 overflow-y-auto p-4 space-y-3">
        {messages.length === 0 && (
          <p className="text-sm text-muted">
            Describe the workflow you want. The configured agent runtime will
            write and build it, then sync it into the editor.
          </p>
        )}
        {messages.map((m, i) => (
          <div key={i} className={m.role === "user" ? "text-right" : ""}>
            <div
              className={
                "inline-block rounded-2xl px-3 py-2 text-sm whitespace-pre-wrap max-w-full text-left " +
                (m.role === "user"
                  ? "bg-brand text-zinc-900"
                  : "bg-elevated text-fg font-mono text-[12px]")
              }
            >
              {m.text || (running && i === messages.length - 1 ? "…" : "")}
            </div>
          </div>
        ))}
      </div>

      <div className="shrink-0 p-3 border-t border-border">
        <div className="flex items-end gap-2">
          <textarea
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                send();
              }
            }}
            placeholder={
              name
                ? "e.g. add a refund branch after payment check"
                : "Name the workflow first"
            }
            rows={2}
            disabled={!name || running}
            className="flex-1 resize-none rounded-lg border border-border bg-bg px-3 py-2 text-sm text-fg placeholder:text-muted focus:outline-none focus:ring-2 focus:ring-brand/40 disabled:opacity-60"
          />
          <button
            onClick={send}
            disabled={!input.trim() || !name || running}
            className="h-9 px-3 rounded-lg text-sm font-semibold bg-brand text-zinc-900 hover:bg-brand-300 disabled:opacity-50"
          >
            {running ? "…" : "Send"}
          </button>
        </div>
      </div>
    </div>
  );
}
