import * as React from "react";
import { NavLink, Outlet } from "react-router-dom";
import {
  Activity,
  HardDrive,
  Megaphone,
  ScrollText,
  Send,
  Smartphone,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { ThemeToggle } from "@/components/theme-toggle";
import { useEnv } from "@/hooks/use-env";
import { cn } from "@/lib/cn";

type NavItem = {
  to: string;
  label: string;
  icon: React.ComponentType<{ className?: string }>;
  devOnly?: boolean;
};

const NAV: NavItem[] = [
  { to: "/", label: "Status", icon: Activity },
  { to: "/logs", label: "Logs", icon: ScrollText },
  { to: "/backup", label: "Backup", icon: HardDrive },
  { to: "/announcement", label: "Announcement", icon: Megaphone },
  { to: "/custom-push", label: "Custom push", icon: Send },
  {
    to: "/test",
    label: "Apple test push",
    icon: Smartphone,
    devOnly: true,
  },
];

export function Layout() {
  const env = useEnv();
  const isDev = env.data?.env === "development";
  const items = NAV.filter((n) => !n.devOnly || isDev);

  return (
    <div className="flex min-h-screen bg-background">
      <aside className="sticky top-0 hidden h-screen w-60 shrink-0 flex-col border-r border-border bg-card/30 lg:flex">
        <div className="flex items-center gap-2.5 px-5 pb-3 pt-5">
          <img
            src="/static/tigerduck-logo.png"
            alt=""
            className="h-7 w-7 rounded"
          />
          <div className="leading-tight">
            <div className="text-sm font-semibold">TigerDuck</div>
            <div className="text-xs text-muted-foreground">Backend Portal</div>
          </div>
        </div>
        <nav className="flex flex-col gap-0.5 px-2 py-3">
          {items.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.to === "/"}
              className={({ isActive }) =>
                cn(
                  "flex items-center gap-2.5 rounded-md px-3 py-2 text-sm font-medium transition-colors",
                  "text-muted-foreground hover:bg-accent hover:text-foreground",
                  isActive && "bg-accent text-foreground",
                )
              }
            >
              <item.icon className="h-4 w-4" />
              {item.label}
            </NavLink>
          ))}
        </nav>
        <div className="mt-auto space-y-3 border-t border-border px-3 py-4">
          <div className="space-y-1.5 px-2 text-xs text-muted-foreground">
            <div className="flex items-center gap-2">
              <span>Mode</span>
              <EnvBadge env={env.data?.env} />
            </div>
            {env.data?.apns_env ? (
              <div className="flex items-center gap-2">
                <span>APNs</span>
                <EnvBadge env={env.data.apns_env} />
              </div>
            ) : null}
          </div>
          <ThemeToggle />
        </div>
      </aside>

      <div className="flex min-w-0 flex-1 flex-col">
        <MobileNav items={items} />
        <main className="flex-1 px-4 py-6 sm:px-8 sm:py-8">
          <div className="mx-auto w-full max-w-6xl space-y-8">
            <Outlet />
          </div>
        </main>
      </div>
    </div>
  );
}

function EnvBadge({ env }: { env: string | undefined }) {
  if (!env) return <Badge variant="muted">unknown</Badge>;
  if (env === "production") return <Badge variant="warning">prod</Badge>;
  if (env === "development") return <Badge variant="success">dev</Badge>;
  return <Badge variant="muted">{env}</Badge>;
}

function MobileNav({ items }: { items: NavItem[] }) {
  return (
    <header className="flex items-center gap-3 border-b border-border bg-card/30 px-4 py-3 lg:hidden">
      <img
        src="/static/tigerduck-logo.png"
        alt=""
        className="h-7 w-7 rounded"
      />
      <div className="flex-1 leading-tight">
        <div className="text-sm font-semibold">TigerDuck Backend Portal</div>
      </div>
      <select
        className="rounded-md border border-input bg-background px-2 py-1 text-sm"
        onChange={(e) => {
          window.location.href = e.target.value;
        }}
        value={window.location.pathname}
      >
        {items.map((it) => (
          <option key={it.to} value={it.to}>
            {it.label}
          </option>
        ))}
      </select>
      <div className="w-32">
        <ThemeToggle />
      </div>
    </header>
  );
}
