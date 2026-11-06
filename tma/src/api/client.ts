import { getAccessToken, refresh } from "../auth/initData";

const API_BASE = import.meta.env.VITE_API_BASE ?? "/api/v1";
const TIMEOUT_MS = 15_000;

export interface ApiError {
  status: number;
  message: string;
}

export function isApiError(value: unknown): value is ApiError {
  return typeof value === "object" && value !== null && "status" in value && "message" in value;
}

export type Status = "pending" | "active" | "suspended" | "expired" | "banned";

export interface Me {
  telegram_id: number;
  username: string | null;
  first_name: string | null;
  status: Status;
  expires_at: number | null;
  offer_accepted: boolean;
  referral_link: string;
  referral_enabled: boolean;
}

export interface Subscription {
  status: Status;
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

export interface PaymentRow {
  payment_id: string;
  amount: number;
  currency: "RUB" | "USDT";
  months: number;
  status: "pending" | "paid" | "cancelled";
  created_at: number;
}

export interface Referral {
  enabled: boolean;
  link: string;
  invited_count: number;
  bonus_days: number;
  inviter_bonus_days: number;
}

async function request(path: string, init: RequestInit, token: string | null): Promise<Response> {
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), TIMEOUT_MS);
  const headers = new Headers(init.headers);
  if (token) headers.set("Authorization", `Bearer ${token}`);
  if (init.body) headers.set("Content-Type", "application/json");
  try {
    return await fetch(`${API_BASE}${path}`, { ...init, headers, signal: controller.signal, credentials: "omit" });
  } catch (err) {
    const timeout = err instanceof DOMException && err.name === "AbortError";
    throw { status: 0, message: timeout ? "timeout" : "network" } satisfies ApiError;
  } finally {
    window.clearTimeout(timer);
  }
}

async function toError(response: Response): Promise<ApiError> {
  try {
    const body = (await response.json()) as { error?: unknown };
    return { status: response.status, message: typeof body.error === "string" ? body.error : "error" };
  } catch {
    return { status: response.status, message: "error" };
  }
}

/** All API calls go through here: auth header, one silent refresh on 401, 15 s timeout. */
export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const sent = getAccessToken();
  let response = await request(path, init, sent);
  if (response.status === 401) {
    const current = getAccessToken();
    const refreshed = current !== null && current !== sent ? true : await refresh();
    if (!refreshed) throw { status: 401, message: "unauthorized" } satisfies ApiError;
    response = await request(path, init, getAccessToken());
  }
  if (!response.ok) throw await toError(response);
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export const endpoints = {
  me: () => api<Me>("/me"),
  subscription: () => api<Subscription>("/subscription"),
  config: () => api<NodeConfig>("/config"),
  tariffs: () => api<{ tiers: Tier[] }>("/tariffs"),
  history: () => api<PaymentRow[]>("/payments/history"),
  referral: () => api<Referral>("/referral"),
  offer: () => api<{ text: string }>("/legal/offer"),
  accept: () => api<{ accepted: boolean; trial: string }>("/legal/accept", { method: "POST", body: "{}" }),
  logout: () => api<void>("/auth/logout", { method: "POST", body: "{}" }),
};
