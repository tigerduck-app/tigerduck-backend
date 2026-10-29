import * as React from "react";
import { NavLink, Outlet, useLocation } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import {
  Activity,
  Calendar,
  FileText,
  GraduationCap,
  HardDrive,
  Link,
  List,
  Lock,
  LogOut,
  Megaphone,
  PanelLeftClose,
  PanelLeftOpen,
  Search,
  Send,
  Server,
  Smartphone,
  UserMinus,
  type LucideIcon,
} from "lucide-react";
import { ThemeToggle } from "@/components/theme-toggle";
import { useEnv } from "@/hooks/use-env";
import { api } from "@/lib/api";
import { cn } from "@/lib/cn";
import type { AccessSession, BackendVersion, EnvInfo } from "@/types/api";

type NavItem = { to: string; label: string; icon: LucideIcon };

// Course changes joins Academic once its page exists.
const NAV: { group: string; items: NavItem[] }[] = [
  {
    group: "Overview",
    items: [
      { to: "/", label: "Status", icon: Activity },
      { to: "/logs", label: "Logs", icon: FileText },
    ],
  },
  {
    group: "Push",
    items: [
      { to: "/announcement", label: "Announcement", icon: Megaphone },
      { to: "/custom-push", label: "Custom push", icon: Send },
      { to: "/lists", label: "Device lists", icon: List },
    ],
  },
  {
    group: "Devices",
    items: [
      { to: "/devices", label: "Devices", icon: Smartphone },
      { to: "/deregister", label: "Deregister", icon: UserMinus },
    ],
  },
  {
    group: "Academic",
    items: [{ to: "/semester-dates", label: "Semester dates", icon: Calendar }],
  },
  {
    group: "Moodle",
    items: [
      { to: "/moodle", label: "Sync", icon: GraduationCap },
      { to: "/inspect", label: "Data inspection", icon: Search },
    ],
  },
  {
    group: "System",
    items: [
      { to: "/stack", label: "Stack", icon: Server },
      { to: "/links", label: "Links", icon: Link },
      { to: "/tls-pins", label: "TLS pins", icon: Lock },
      { to: "/backup", label: "Backup", icon: HardDrive },
    ],
  },
];

export function Layout() {
  const env = useEnv();
  const [collapsed, setCollapsed] = React.useState(() => {
    try { return localStorage.getItem("sidebar-collapsed") === "true"; } catch { return false; }
  });
  React.useEffect(() => {
    try { localStorage.setItem("sidebar-collapsed", String(collapsed)); } catch {}
  }, [collapsed]);

  // The phone top bar scrolls sideways; keep the current page's label in view.
  const bar = React.useRef<HTMLDivElement>(null);
  const { pathname } = useLocation();
  React.useEffect(() => {
    const el = bar.current;
    const current = el?.querySelector<HTMLElement>('[aria-current="page"]');
    if (!el || !current || el.scrollWidth <= el.clientWidth) return;
    const offset = current.getBoundingClientRect().left - el.getBoundingClientRect().left;
    el.scrollLeft += offset - (el.clientWidth - current.offsetWidth) / 2;
  }, [pathname]);

  return (
    <div className="@container/app flex min-h-screen flex-wrap content-start bg-ground font-sans text-sm leading-[1.45] text-ink">
      <nav
        aria-label="Portal"
        data-rail={collapsed}
        className="min-w-0 flex-[1_1_248px] bg-side text-side-ink rail:flex-[0_0_68px] phone:flex-[1_1_100%]"
      >
        <div
          ref={bar}
          className="sticky top-0 flex h-screen flex-col gap-5 overflow-y-auto px-3.5 py-5 [scrollbar-color:var(--color-side-chip)_transparent] [scrollbar-width:thin] phone:[scrollbar-width:none] rail:items-center rail:gap-3.5 rail:px-2.5 rail:py-4 phone:static phone:h-auto phone:flex-row phone:gap-1 phone:overflow-x-auto phone:overflow-y-visible phone:px-3 phone:py-2"
        >
          <Brand collapsed={collapsed} onToggle={() => setCollapsed((c) => !c)} />
          {env.data && <EnvCard env={env.data} />}
          {NAV.map((g) => (
            <div
              key={g.group}
              className="flex flex-col gap-0.5 rail:items-center phone:flex-row"
            >
              <span className="px-2.5 pb-1 text-[11px] font-semibold uppercase tracking-[0.08em] text-side-muted rail:hidden">
                {g.group}
              </span>
              {g.items.map((item) => (
                <NavItemLink key={item.to} item={item} />
              ))}
            </div>
          ))}
          <UserRow access={env.data?.access ?? null} />
        </div>
      </nav>

      <main
        className="min-w-0 flex-[999_1_560px] px-10 pb-16 pt-8 tablet:px-7 tablet:pb-14 tablet:pt-7 phone:px-4 phone:pb-12 phone:pt-5"
        style={{ overflowAnchor: "none" }}
      >
        <div className="flex w-full max-w-[1180px] flex-col gap-6">
          <Outlet />
        </div>
      </main>
    </div>
  );
}

function Brand({ collapsed, onToggle }: { collapsed: boolean; onToggle: () => void }) {
  const version = useQuery<BackendVersion>({
    queryKey: ["version"],
    queryFn: () => api<BackendVersion>("/api/version"),
    staleTime: Infinity,
  });
  const label = collapsed ? "Expand sidebar" : "Collapse sidebar";
  const Icon = collapsed ? PanelLeftOpen : PanelLeftClose;
  return (
    <div className="flex items-center gap-2.5 px-1.5 rail:flex-col rail:px-0 phone:flex-row">
      <div
        aria-hidden="true"
        className="flex size-[30px] shrink-0 items-center justify-center rounded-lg bg-brand text-[15px] font-bold text-side"
      >
        T
      </div>
      <div className="flex min-w-0 flex-col rail:hidden">
        <div className="flex items-center gap-1.5">
          <span className="text-sm font-semibold text-white">TigerDuck</span>
          {version.data?.ok && version.data.version && (
            <span
              title="Backend version"
              className="rounded-md bg-side-chip px-1.5 font-mono text-[11px] leading-4 text-side-ink"
            >
              {version.data.version}
            </span>
          )}
        </div>
        <span className="text-xs text-side-muted">Backend portal</span>
      </div>
      <button
        type="button"
        aria-label={label}
        title={label}
        onClick={onToggle}
        className="ml-auto flex size-8 shrink-0 items-center justify-center rounded-lg text-side-muted hover:bg-side-hover hover:text-white rail:ml-0 tablet:hidden"
      >
        <Icon className="h-4 w-4" />
      </button>
    </div>
  );
}

function capitalize(s: string): string {
  return s.charAt(0).toUpperCase() + s.slice(1);
}

function EnvCard({ env }: { env: EnvInfo }) {
  const { apns_config: apns, fcm_config: fcm } = env;
  const dot =
    env.env === "production" ? "bg-brand" : env.env === "development" ? "bg-dot-ok" : "bg-dot-off";
  // Same values as the Status page's summary cards.
  const apnsValue = apns.state === "ok" ? apns.apns_env : "disabled";
  const fcmValue =
    fcm.state === "ok" ? fcm.project_id : fcm.state === "mismatch" ? "mismatch" : "disabled";
  return (
    <div className="flex flex-col gap-1.5 rounded-[10px] bg-side-raised px-3 py-2.5 text-xs rail:hidden">
      <div className="flex items-center gap-2 font-medium text-white">
        <span aria-hidden="true" className={cn("size-2 rounded-full", dot)} />
        {capitalize(env.env || "unknown")}
      </div>
      <EnvRow label="APNs" value={apnsValue} detail={apns.state === "ok" ? undefined : apns.detail} />
      <EnvRow label="FCM" value={fcmValue} detail={fcm.state === "ok" ? undefined : fcm.detail} />
    </div>
  );
}

function EnvRow({ label, value, detail }: { label: string; value?: string; detail?: string }) {
  return (
    <div className="flex justify-between gap-3 text-side-muted">
      <span>{label}</span>
      <span title={detail} className="min-w-0 truncate text-side-ink">
        {value || "?"}
      </span>
    </div>
  );
}

function NavItemLink({ item }: { item: NavItem }) {
  const Icon = item.icon;
  return (
    <NavLink
      to={item.to}
      end={item.to === "/"}
      title={item.label}
      aria-label={item.label}
      className={({ isActive }) =>
        cn(
          "flex min-h-9 shrink-0 items-center gap-2.5 rounded-lg px-2.5 text-sm no-underline",
          "rail:h-10 rail:w-11 rail:justify-center rail:p-0",
          isActive
            ? "bg-side-active font-medium text-white phone:w-auto phone:px-3"
            : "text-side-ink hover:bg-side-hover hover:text-white",
        )
      }
    >
      {({ isActive }) => (
        <>
          <Icon className={cn("h-4 w-4 shrink-0", isActive && "text-brand")} />
          <span className={cn("rail:hidden", isActive && "phone:inline")}>{item.label}</span>
        </>
      )}
    </NavLink>
  );
}

function UserRow({ access }: { access: AccessSession | null }) {
  return (
    <div className="mt-auto flex items-center gap-2.5 border-t border-side-chip px-1.5 pt-3.5 rail:flex-col rail:gap-1.5 rail:px-0 rail:pt-3 phone:ml-auto phone:mt-0 phone:flex-row phone:border-t-0 phone:pl-2 phone:pt-0">
      {access && (
        <>
          <div
            aria-hidden="true"
            className="flex size-8 shrink-0 items-center justify-center rounded-full bg-side-active text-[13px] font-semibold text-white phone:hidden"
          >
            {access.email.charAt(0).toUpperCase()}
          </div>
          <div className="flex min-w-0 flex-col rail:hidden">
            <span title={access.email} className="truncate text-[13px] text-white">
              {access.email}
            </span>
            <span className="text-xs text-side-muted">Cloudflare Access</span>
          </div>
        </>
      )}
      <ThemeToggle
        iconOnly
        className="ml-auto size-9 shrink-0 rounded-lg text-side-ink hover:bg-side-hover hover:text-white rail:ml-0"
      />
      {access && <SignOutLink access={access} />}
    </div>
  );
}

// Cloudflare answers the logout path at its edge, so this must be a full
// page load. A react-router link would resolve it inside the SPA instead,
// and the request would never reach Cloudflare.
function SignOutLink({ access }: { access: AccessSession }) {
  return (
    <a
      href={access.logout_url}
      aria-label="Sign out"
      title={`Sign ${access.email} out of Cloudflare Access`}
      className="flex size-9 shrink-0 items-center justify-center rounded-lg text-side-ink hover:bg-side-hover hover:text-white"
    >
      <LogOut className="h-4 w-4" />
    </a>
  );
}
