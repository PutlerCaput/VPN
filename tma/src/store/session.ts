import { create } from "zustand";

export type Route = "dashboard" | "connect" | "pay" | "referral" | "profile" | "legal";
export type AuthState = "booting" | "ready" | "expired" | "offline" | "outside";

interface SessionState {
  auth: AuthState;
  route: Route;
  setAuth: (auth: AuthState) => void;
  navigate: (route: Route) => void;
}

export const useSession = create<SessionState>((set) => ({
  auth: "booting",
  route: "dashboard",
  setAuth: (auth) => set({ auth }),
  navigate: (route) => set({ route }),
}));
