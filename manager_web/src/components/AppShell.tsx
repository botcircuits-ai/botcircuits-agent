"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { GITHUB_URL } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { useTheme } from "@/lib/theme";
import { cx } from "@/lib/format";
import { Logo } from "./Logo";
import {
  GithubIcon,
  MoonIcon,
  SignOutIcon,
  SunIcon,
  TraceIcon,
  WorkflowIcon,
} from "./icons";

type NavItem = {
  label: string;
  href: string;
  icon: React.ReactNode;
  soon?: boolean;
};

const NAV: NavItem[] = [
  { label: "Tracing", href: "/tracing", icon: <TraceIcon /> },
  { label: "Workflows", href: "/workflows", icon: <WorkflowIcon />, soon: true },
];

/** The authenticated app frame: left nav + top bar + content slot. */
export function AppShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const { signOut } = useAuth();
  const { theme, toggle } = useTheme();

  return (
    <div className="min-h-screen grid grid-cols-[15rem_1fr] max-md:grid-cols-1">
      {/* Sidebar */}
      <aside className="border-r border-border bg-surface flex flex-col max-md:hidden">
        <div className="h-16 flex items-center px-5 border-b border-border">
          <Logo />
        </div>
        <nav className="flex-1 p-3 space-y-1">
          <p className="px-3 py-2 text-xs font-medium uppercase tracking-wider text-muted">
            Platform
          </p>
          {NAV.map((item) => {
            const active = pathname.startsWith(item.href);
            const content = (
              <span
                className={cx(
                  "flex items-center gap-3 px-3 py-2 rounded-xl text-sm font-medium",
                  active
                    ? "bg-brand/15 text-fg ring-1 ring-brand/30"
                    : "text-muted hover:text-fg hover:bg-elevated",
                  item.soon && "opacity-60 cursor-default",
                )}
              >
                <span className={active ? "text-brand-600 dark:text-brand-400" : ""}>
                  {item.icon}
                </span>
                {item.label}
                {item.soon && (
                  <span className="ml-auto text-[10px] uppercase tracking-wide text-muted border border-border rounded-full px-1.5 py-0.5">
                    Soon
                  </span>
                )}
              </span>
            );
            return item.soon ? (
              <div key={item.href}>{content}</div>
            ) : (
              <Link key={item.href} href={item.href}>
                {content}
              </Link>
            );
          })}
        </nav>
        <div className="p-3 border-t border-border text-xs text-muted">
          BotCircuits Manager · v0.1
        </div>
      </aside>

      {/* Main column */}
      <div className="flex flex-col min-w-0">
        <header className="h-16 border-b border-border bg-surface/80 backdrop-blur flex items-center gap-2 px-5 sticky top-0 z-10">
          <div className="md:hidden mr-2">
            <Logo compact />
          </div>
          <div className="flex-1" />
          <a
            href={GITHUB_URL}
            target="_blank"
            rel="noreferrer"
            className="inline-flex items-center gap-2 h-9 px-3 rounded-lg text-sm text-muted hover:text-fg hover:bg-elevated"
            title="View on GitHub"
          >
            <GithubIcon className="w-[18px] h-[18px]" />
            <span className="max-sm:hidden">GitHub</span>
          </a>
          <button
            onClick={toggle}
            className="inline-flex items-center justify-center h-9 w-9 rounded-lg text-muted hover:text-fg hover:bg-elevated"
            title={theme === "dark" ? "Switch to light" : "Switch to dark"}
            aria-label="Toggle theme"
          >
            {theme === "dark" ? <SunIcon className="w-[18px] h-[18px]" /> : <MoonIcon className="w-[18px] h-[18px]" />}
          </button>
          <button
            onClick={signOut}
            className="inline-flex items-center gap-2 h-9 px-3 rounded-lg text-sm text-muted hover:text-danger hover:bg-danger/10"
            title="Sign out"
          >
            <SignOutIcon className="w-[18px] h-[18px]" />
            <span className="max-sm:hidden">Sign out</span>
          </button>
        </header>

        <main className="flex-1 min-w-0 p-6 max-w-[100rem] w-full mx-auto">
          {children}
        </main>
      </div>
    </div>
  );
}
