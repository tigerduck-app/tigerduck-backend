import * as React from "react";
import { NavLink, Outlet } from "react-router-dom";
import {
  Activity,
  CalendarDays,
  GraduationCap,
  HardDrive,
  Menu,
  Megaphone,
  Monitor,
  ScrollText,
  Search,
  Send,
  UserMinus,
  X,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { ThemeToggle } from "@/components/theme-toggle";
import { useEnv } from "@/hooks/use-env";
import { cn } from "@/lib/cn";
import type { ApnsConfig, FcmConfig } from "@/types/api";

type NavItem = {
  to: string;
  label: string;
  icon: React.ComponentType<{ className?: string }>;
  devOnly?: boolean;
};

const NAV: NavItem[] = [
  { to: "/", label: "Status", icon: Activity },
  { to: "/logs", label: "Logs", icon: ScrollText },
  { to: "/announcement", label: "Announcement", icon: Megaphone },
  { to: "/custom-push", label: "Custom push", icon: Send },
  { to: "/devices", label: "Devices", icon: Monitor },
  { to: "/moodle", label: "Moodle", icon: GraduationCap },
  { to: "/inspect", label: "Data Inspection", icon: Search },
  { to: "/semester-dates", label: "Semester dates", icon: CalendarDays },
];

const NAV_BOTTOM: NavItem[] = [
  { to: "/backup", label: "Backup", icon: HardDrive },
  { to: "/deregister", label: "Deregister", icon: UserMinus },
];

export function Layout() {
  const env = useEnv();
  const isDev = env.data?.env === "development";
  const items = NAV.filter((n) => !n.devOnly || isDev);
  const bottomItems = NAV_BOTTOM.filter((n) => !n.devOnly || isDev);
  const [sidebarCollapsed, setSidebarCollapsed] = React.useState(() => {
    try { return localStorage.getItem("sidebar-collapsed") === "true"; } catch { return false; }
  });
  React.useEffect(() => {
    try { localStorage.setItem("sidebar-collapsed", String(sidebarCollapsed)); } catch {}
  }, [sidebarCollapsed]);

  return (
    <div className="flex min-h-screen bg-background">
      <aside className={`sticky top-0 hidden h-screen shrink-0 flex-col border-r border-border bg-card/30 lg:flex transition-all duration-200 ${sidebarCollapsed ? "w-14" : "w-60"}`}>
        <div className={`flex items-center gap-2.5 pb-3 pt-5 ${sidebarCollapsed ? "justify-center px-2" : "px-5"}`}>
          {sidebarCollapsed ? (
            // Stacked rather than side by side: 56px minus padding leaves 40px,
            // which the 28px logo and a tap target cannot share.
            <div className="flex flex-col items-center gap-4">
              <img
                src="/static/tigerduck-logo.png"
                alt=""
                className="h-7 w-7 rounded"
              />
              <button
                onClick={() => setSidebarCollapsed(false)}
                className="rounded-md p-1.5 hover:bg-accent text-muted-foreground"
                title="Expand sidebar"
              >
                <Menu className="h-4 w-4" />
              </button>
            </div>
          ) : (
            <>
              <img
                src="/static/tigerduck-logo.png"
                alt=""
                className="h-7 w-7 rounded shrink-0"
              />
              <div className="leading-tight">
                <div className="text-sm font-semibold">TigerDuck</div>
                <div className="text-xs text-muted-foreground">Backend Portal</div>
              </div>
              <button
                onClick={() => setSidebarCollapsed(true)}
                className="ml-auto rounded-md p-2.5 -m-1.5 hover:bg-accent text-muted-foreground"
                title="Collapse sidebar"
              >
                <X className="h-4 w-4" />
              </button>
            </>
          )}
        </div>
        <nav className={`flex flex-col gap-0.5 py-3 ${sidebarCollapsed ? "px-1" : "px-2"}`}>
          {items.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.to === "/"}
              title={sidebarCollapsed ? item.label : undefined}
              className={({ isActive }) =>
                cn(
                  "flex items-center rounded-md text-sm font-medium transition-colors",
                  sidebarCollapsed ? "justify-center px-2 py-2" : "gap-2.5 px-3 py-2",
                  "text-muted-foreground hover:bg-accent hover:text-foreground",
                  isActive && "bg-accent text-foreground",
                )
              }
            >
              <item.icon className="h-4 w-4 shrink-0" />
              {!sidebarCollapsed && item.label}
            </NavLink>
          ))}
        </nav>
        <nav className={`mt-auto flex flex-col gap-0.5 border-t border-border py-3 ${sidebarCollapsed ? "px-1" : "px-2"}`}>
          {bottomItems.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              title={sidebarCollapsed ? item.label : undefined}
              className={({ isActive }) =>
                cn(
                  "flex items-center rounded-md text-sm font-medium transition-colors",
                  sidebarCollapsed ? "justify-center px-2 py-2" : "gap-2.5 px-3 py-2",
                  "text-muted-foreground hover:bg-accent hover:text-foreground",
                  isActive && "bg-accent text-foreground",
                )
              }
            >
              <item.icon className="h-4 w-4 shrink-0" />
              {!sidebarCollapsed && item.label}
            </NavLink>
          ))}
        </nav>
        <div className={`border-t border-border py-4 ${sidebarCollapsed ? "px-1" : "px-3"}`}>
          {!sidebarCollapsed && (
            <div className="space-y-1.5 px-2 text-xs text-foreground/80 mb-3">
              <div className="flex items-center gap-2">
                <span className="font-medium">Mode</span>
                <EnvBadge env={env.data?.env} />
              </div>
              <div className="flex items-center gap-2">
                <span className="font-medium">APNs</span>
                <ApnsBadge apns={env.data?.apns_config} />
              </div>
              <div className="flex items-center gap-2">
                <span className="font-medium">FCM</span>
                <FcmBadge fcm={env.data?.fcm_config} />
              </div>
            </div>
          )}
          <ThemeToggle iconOnly={sidebarCollapsed} />
        </div>
      </aside>

      <div className="flex min-w-0 flex-1 flex-col">
        <MobileNav items={[...items, ...bottomItems]} />
        <main className="flex-1 px-2 py-4 sm:px-4 sm:py-6" style={{ overflowAnchor: "none" }}>
          <div className="mx-auto w-full space-y-6">
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

// Mirrors the FCM SummaryCard on the status page: project_id when env +
// JSON agree, "mismatch" when both present but differ, "disabled" when
// anything is missing or nothing is configured at all.
function FcmBadge({ fcm }: { fcm: FcmConfig | undefined }) {
  if (!fcm) return <Badge variant="muted">unknown</Badge>;
  if (fcm.state === "ok") {
    return <Badge variant="default">{fcm.project_id}</Badge>;
  }
  if (fcm.state === "mismatch") {
    return (
      <Badge variant="warning" title={fcm.detail}>
        mismatch
      </Badge>
    );
  }
  return (
    <Badge variant="muted" title={fcm.detail}>
      disabled
    </Badge>
  );
}

// APNs has no "mismatch" — the .p8 file is just a private key. When all
// three (team id, key id, file) are present we surface the apns_env;
// otherwise the badge says disabled and the dashboard explains why.
function ApnsBadge({ apns }: { apns: ApnsConfig | undefined }) {
  if (!apns) return <Badge variant="muted">unknown</Badge>;
  if (apns.state !== "ok") {
    return (
      <Badge variant="muted" title={apns.detail}>
        disabled
      </Badge>
    );
  }
  return <EnvBadge env={apns.apns_env} />;
}

function MobileNav({ items }: { items: NavItem[] }) {
  const [open, setOpen] = React.useState(false);

  return (
    <>
      <header className="flex items-center gap-3 border-b border-border bg-card/30 px-4 py-3 lg:hidden">
        <button
          onClick={() => setOpen(!open)}
          className="rounded-md p-1.5 hover:bg-accent"
          aria-label="Toggle menu"
        >
          {open ? <X className="h-5 w-5" /> : <Menu className="h-5 w-5" />}
        </button>
        <img
          src="/static/tigerduck-logo.png"
          alt=""
          className="h-7 w-7 rounded"
        />
        <div className="flex-1 leading-tight">
          <div className="text-sm font-semibold">TigerDuck Backend Portal</div>
        </div>
        <ThemeToggle />
      </header>
      {open && (
        <nav className="border-b border-border bg-card/30 px-2 py-2 lg:hidden">
          {items.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.to === "/"}
              onClick={() => setOpen(false)}
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
      )}
    </>
  );
}
