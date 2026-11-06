import { create } from "zustand";

export type Route = "dashboard" | "pay" | "referral" | "profile" | "legal";

const ROUTES: readonly Route[] = ["dashboard", "pay", "referral", "profile", "legal"];
const REFRESH_KEY = "node.refresh";

function routeFromHash(): Route {
  const name = window.location.hash.replace(/^#\/?/, "");
  if (name === "") return "dashboard";
  return (ROUTES as readonly string[]).includes(name) ? (name as Route) : "dashboard";
}

function readRefresh(): string | null {
  try {
    return window.localStorage.getItem(REFRESH_KEY);
  } catch {
    return null;
  }
}

interface AppState {
  route: Route;
  accessToken: string | null;
  refreshToken: string | null;
  navigate: (route: Route) => void;
  syncRoute: () => void;
  setTokens: (access: string, refresh: string) => void;
  clearTokens: () => void;
}

/**
 * The access token lives in memory only (15 min). The refresh token is kept in
 * localStorage so the installed PWA survives restarts; it is rotated on every
 * refresh and revocable server-side.
 */
export const useAppStore = create<AppState>((set) => ({
  route: routeFromHash(),
  accessToken: null,
  refreshToken: readRefresh(),
  navigate: (route) => {
    window.location.hash = `#/${route === "dashboard" ? "" : route}`;
    set({ route });
  },
  syncRoute: () => set({ route: routeFromHash() }),
  setTokens: (access, refresh) => {
    try {
      window.localStorage.setItem(REFRESH_KEY, refresh);
    } catch {
      /* storage unavailable: session stays in memory */
    }
    set({ accessToken: access, refreshToken: refresh });
  },
  clearTokens: () => {
    try {
      window.localStorage.removeItem(REFRESH_KEY);
    } catch {
      /* ignore */
    }
    set({ accessToken: null, refreshToken: null });
  },
}));
