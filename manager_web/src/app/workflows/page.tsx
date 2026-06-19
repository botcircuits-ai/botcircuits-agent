"use client";

import { RequireAuth } from "@/components/RequireAuth";
import { WorkflowIcon } from "@/components/icons";

export default function WorkflowsPage() {
  return (
    <RequireAuth>
      <div className="grid place-items-center min-h-[60vh] text-center">
        <div className="max-w-md">
          <div className="mx-auto mb-4 grid place-items-center h-14 w-14 rounded-2xl bg-brand/15 text-brand-600 dark:text-brand-400">
            <WorkflowIcon className="w-7 h-7" />
          </div>
          <h1 className="text-xl font-semibold text-fg">Workflows</h1>
          <p className="mt-2 text-sm text-muted">
            The workflow manager and editor will live here — browse, create, and
            edit workflows visually. Coming in a future release.
          </p>
        </div>
      </div>
    </RequireAuth>
  );
}
