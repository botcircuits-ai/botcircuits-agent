/**
 * Typed client for the BotCircuits Manager backend.
 *
 * Base URL comes from NEXT_PUBLIC_API_BASE (default localhost:8700). The bearer
 * token is held in localStorage by the auth layer and passed in per call.
 */

export const API_BASE =
  process.env.NEXT_PUBLIC_API_BASE ?? "http://127.0.0.1:8700";

export const GITHUB_URL =
  process.env.NEXT_PUBLIC_GITHUB_URL ??
  "https://github.com/botcircuits-ai/botcircuits-agent";

export type SessionSummary = {
  session_id: string;
  workflow: string | null;
  runtime: string | null;
  start: string | null;
  end: string | null;
  status: "running" | "paused" | "done" | "failure" | string;
  event_count: number;
  updated_at: number;
};

export type TraceEvent = {
  seq: number;
  ts: string;
  type: string;
  step: string | null;
  duration_ms: number | null;
  slots: Record<string, unknown>;
  data: Record<string, unknown>;
};

export type MemoryNode = {
  id: string;
  kind: string;
  label?: string;
  value?: unknown;
  [k: string]: unknown;
};

export type MemoryEdge = {
  from: string;
  to: string;
  kind?: string;
  [k: string]: unknown;
};

export type SessionDoc = {
  session_id: string;
  agent: { runtime?: string };
  workflow: {
    name?: string;
    start?: string;
    end?: string | null;
    initial_slots?: Record<string, unknown>;
  };
  trace: TraceEvent[];
  memory: { nodes: MemoryNode[]; edges: MemoryEdge[] };
};

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function request<T>(
  path: string,
  opts: { token?: string | null; method?: string; body?: unknown } = {},
): Promise<T> {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (opts.token) headers["Authorization"] = `Bearer ${opts.token}`;

  let res: Response;
  try {
    res = await fetch(`${API_BASE}${path}`, {
      method: opts.method ?? "GET",
      headers,
      body: opts.body ? JSON.stringify(opts.body) : undefined,
      cache: "no-store",
    });
  } catch {
    throw new ApiError(0, `Cannot reach the manager backend at ${API_BASE}.`);
  }

  if (!res.ok) {
    let detail = res.statusText;
    try {
      const j = await res.json();
      detail = (j && (j.detail || j.message)) || detail;
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(res.status, detail);
  }
  return (await res.json()) as T;
}

export const api = {
  health: () =>
    request<{ status: string; auth_configured: boolean }>("/api/health"),

  login: (username: string, password: string) =>
    request<{ token: string; expires_in: number }>("/api/auth/login", {
      method: "POST",
      body: { username, password },
    }),

  listSessions: (token: string) =>
    request<SessionSummary[]>("/api/sessions", { token }),

  getSession: (token: string, id: string) =>
    request<SessionDoc>(`/api/sessions/${encodeURIComponent(id)}`, { token }),
};
