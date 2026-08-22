import { useCallback, useSyncExternalStore } from "react";

function getHash() {
  return window.location.hash.replace("#", "") || "";
}

function subscribe(cb: () => void) {
  window.addEventListener("hashchange", cb);
  return () => window.removeEventListener("hashchange", cb);
}

export function useHashTab(defaultValue: string): [string, (v: string) => void] {
  const hash = useSyncExternalStore(subscribe, getHash, () => "");
  const value = hash || defaultValue;
  const setValue = useCallback((v: string) => {
    window.history.replaceState(null, "", `#${v}`);
    window.dispatchEvent(new HashChangeEvent("hashchange"));
  }, []);
  return [value, setValue];
}
