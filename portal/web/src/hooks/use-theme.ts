import { useCallback, useEffect, useState } from "react";

export type Theme = "system" | "light" | "dark";
export type ResolvedTheme = "light" | "dark";

const STORAGE_KEY = "tigerduck.portal.theme";

function resolve(theme: Theme): ResolvedTheme {
  if (theme === "system") {
    return window.matchMedia("(prefers-color-scheme: dark)").matches
      ? "dark"
      : "light";
  }
  return theme;
}

function apply(resolved: ResolvedTheme): void {
  document.documentElement.classList.toggle("dark", resolved === "dark");
  // colorScheme tells the UA to pick the matching native scrollbar /
  // form control palette — without it, the browser scrollbar stays
  // light even when our chrome goes dark.
  document.documentElement.style.colorScheme = resolved;
}

function readStored(): Theme {
  try {
    const v = localStorage.getItem(STORAGE_KEY);
    if (v === "light" || v === "dark" || v === "system") return v;
  } catch {
    // localStorage unavailable (private mode etc.); fall through.
  }
  return "system";
}

export function useTheme() {
  const [theme, setThemeState] = useState<Theme>(() => readStored());
  const [resolved, setResolved] = useState<ResolvedTheme>(() =>
    resolve(readStored()),
  );

  const setTheme = useCallback((next: Theme) => {
    setThemeState(next);
    try {
      localStorage.setItem(STORAGE_KEY, next);
    } catch {
      // Best-effort persistence; the inline script in index.html will
      // also fall back to the default on next load.
    }
    const r = resolve(next);
    setResolved(r);
    apply(r);
  }, []);

  // Re-resolve when the OS theme changes, but only while we're in
  // "system" mode — an explicit user pick should stick regardless of
  // what macOS / Windows decides to do at sunset.
  useEffect(() => {
    if (theme !== "system") return;
    const mq = window.matchMedia("(prefers-color-scheme: dark)");
    const onChange = () => {
      const r: ResolvedTheme = mq.matches ? "dark" : "light";
      setResolved(r);
      apply(r);
    };
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, [theme]);

  return { theme, resolved, setTheme };
}
