import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { EnvInfo } from "@/types/api";

export function useEnv() {
  return useQuery<EnvInfo>({
    queryKey: ["env"],
    queryFn: () => api<EnvInfo>("/api/env"),
    staleTime: Infinity,
    // The payload names the Cloudflare Access user, which can change while
    // this tab stays open: another operator signing in from a second tab
    // replaces the session cookie. Ask again whenever the tab comes back to
    // the foreground, overriding the app-wide refetchOnWindowFocus: false.
    refetchOnWindowFocus: "always",
  });
}
