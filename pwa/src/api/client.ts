import { useAppStore } from "../store";

const API_BASE = import.meta.env.VITE_API_BASE ?? "/api/v1";
const TIMEOUT_MS = 15_000;

export class ApiError extends Error {
  constructor(
    public readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

export interface TokenPair {
  access_token: string;
  refresh_token: string;
  expires_in: number;
}

export interface Me {
  telegram_id: number;
  username: string | null;
  first_name: string | null;
  status: "pending" | "active" | "suspended" | "expired" | "banned";
  expires_at: number | null;
  node_username: string | null;
  offer_accepted: boolean;
  trial_used: boolean;
  referral_count: number;
  referral_link: string;
  referral_enabled: boolean;
  bot_username: string;
}

export interface Subscription {
  status: Me["status"];
  expires_at: number | null;
  days_remaining: number;
  node_username: string | null;
}

export interface NodeConfig {
  config_link: string;
  qr_payload: string;
  expires_at: number | null;
}

export interface TierPrice {
  months: number;
  rub: number;
  usdt: string;
  save_percent: number;
}

export interface Tier {
  key: string;
  title: string;
  devices: number;
  priority_support: boolean;
  features: string[];
  prices: TierPrice[];
}

export interface SbpPayment {
  payment_id: string;
  qr_url: string;
  payment_url: string;
  amount_rub: number;
  manual: boolean;
  provider?: string;
  receipt?: string;
}

export interface UsdtPayment {
  payment_id: string;
  invoice_id: string;
  pay_url: string;
  amount_usdt: string;
}

export interface PaymentRow {
  payment_id: string;
  amount: number;
  currency: "RUB" | "USDT";
  months: number;
  tier?: string;
  method: "sbp" | "usdt";
  status: "pending" | "paid" | "cancelled";
  created_at: number;
  paid_at: number | null;
}

export interface Referral {
  enabled: boolean;
  link: string;
  invited_count: number;
  bonus_days: number;
  inviter_bonus_days: number;
  invitee_bonus_days: number;
}

let refreshing: Promise<boolean> | null = null;

async function rawFetch(path: string, init: RequestInit, token: string | null): Promise<Response> {
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), TIMEOUT_MS);
  const headers = new Headers(init.headers);
  if (token) headers.set("Authorization", `Bearer ${token}`);
  if (init.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  try {
    return await fetch(`${API_BASE}${path}`, { ...init, headers, signal: controller.signal, credentials: "omit" });
  } catch (err) {
    throw new ApiError(0, err instanceof DOMException && err.name === "AbortError" ? "timeout" : "network");
  } finally {
    window.clearTimeout(timer);
  }
}

/** Single-flight refresh: parallel 401s share one /auth/refresh call. */
export function refreshSession(): Promise<boolean> {
  if (refreshing) return refreshing;
  const { refreshToken, setTokens, clearTokens } = useAppStore.getState();
  if (!refreshToken) return Promise.resolve(false);
  refreshing = (async () => {
    try {
      const response = await rawFetch(
        "/auth/refresh",
        { method: "POST", body: JSON.stringify({ refresh_token: refreshToken }) },
        null,
      );
      if (!response.ok) {
        clearTokens();
        return false;
      }
      const pair = (await response.json()) as TokenPair;
      setTokens(pair.access_token, pair.refresh_token);
      return true;
    } catch {
      return false;
    } finally {
      refreshing = null;
    }
  })();
  return refreshing;
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const sentWith = useAppStore.getState().accessToken;
  let response = await rawFetch(path, init, sentWith);
  if (response.status === 401) {
    const current = useAppStore.getState().accessToken;
    const ok = current && current !== sentWith ? true : await refreshSession();
    if (!ok) throw new ApiError(401, "unauthorized");
    response = await rawFetch(path, init, useAppStore.getState().accessToken);
  }
  if (response.status === 204) return undefined as T;
  let payload: unknown = null;
  try {
    payload = await response.json();
  } catch {
    payload = null;
  }
  if (!response.ok) {
    const message =
      payload && typeof payload === "object" && "error" in payload ? String((payload as { error: unknown }).error) : "error";
    throw new ApiError(response.status, message);
  }
  return payload as T;
}

export const endpoints = {
  loginWithWidget: (data: Record<string, string | number>) =>
    api<TokenPair>("/auth/telegram", { method: "POST", body: JSON.stringify(data) }),
  logout: (refreshToken: string | null) =>
    api<void>("/auth/logout", { method: "POST", body: JSON.stringify({ refresh_token: refreshToken }) }),
  me: () => api<Me>("/me"),
  subscription: () => api<Subscription>("/subscription"),
  config: () => api<NodeConfig>("/config"),
  tariffs: () => api<{ tiers: Tier[] }>("/tariffs"),
  createPayment: (months: number, method: "sbp" | "usdt", tier: string, email?: string) =>
    api<SbpPayment | UsdtPayment>("/payments/create", {
      method: "POST",
      body: JSON.stringify(email ? { months, method, tier, email } : { months, method, tier }),
    }),
  history: () => api<PaymentRow[]>("/payments/history"),
  referral: () => api<Referral>("/referral"),
  offer: () => api<{ text: string }>("/legal/offer"),
  acceptOffer: () => api<{ accepted: boolean; trial: string; expires_at: number | null }>("/legal/accept", { method: "POST" }),
  vapidKey: () => api<{ public_key: string }>("/push/vapid-key"),
  pushSubscribe: (subscription: PushSubscriptionJSON) =>
    api<{ ok: boolean }>("/push/subscribe", { method: "POST", body: JSON.stringify(subscription) }),
  pushUnsubscribe: (endpoint: string) =>
    api<{ removed: number }>("/push/subscribe", { method: "DELETE", body: JSON.stringify({ endpoint }) }),
};

export function formatDate(ts: number | null): string {
  if (!ts) return "—";
  return new Date(ts * 1000).toLocaleString("ru-RU", { dateStyle: "medium", timeStyle: "short" });
}
