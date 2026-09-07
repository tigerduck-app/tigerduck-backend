import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { EnvInfo } from "@/types/api";

export function useEnv() {
  return useQuery<EnvInfo>({
    queryKey: ["env"],
    queryFn: () => api<EnvInfo>("/api/env"),
    staleTime: Infinity,
  });
}
