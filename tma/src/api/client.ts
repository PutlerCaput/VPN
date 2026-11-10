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
  /** The bot's @name from the server settings: used for chat links. */
  bot_username?: string;
}

export interface Subscription {
  status: Status;
  expires_at: number | null;
  days_remaining: number;
  node_username: string | null;
  /** null when the panel is unreachable; limit_bytes 0 = unlimited. */
  traffic?: TrafficStatus | null;
}

export interface ServerLocation {
  name: string;
  online: boolean;
}

export interface ServersReport {
  checked_at: number;
  panel_ok: boolean;
  subscription: boolean;
  locations: ServerLocation[];
}

export interface TrafficStatus {
  used_bytes: number;
  limit_bytes: number;
  resets_every_30_days: boolean;
  exhausted: boolean;
}

export interface NodeConfig {
  config_link: string;
  qr_payload: string;
  expires_at: number | null;
  /** v2rayNG receives "Russian sites directly" with the subscription. */
  ru_direct_auto?: boolean;
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
  currency: "RUB" | "USDT" | "XTR";
  months: number;
  status: "pending" | "paid" | "cancelled" | "refunded";
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
    const refreshed = current !== null && current !== sent ? "ok" : await refresh();
    // Only a rejected refresh means "logged out"; an outage stays a network error.
    if (refreshed === "unavailable") throw { status: 0, message: "network" } satisfies ApiError;
    if (refreshed !== "ok") throw { status: 401, message: "unauthorized" } satisfies ApiError;
    response = await request(path, init, getAccessToken());
  }
  if (!response.ok) throw await toError(response);
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export const endpoints = {
  me: () => api<Me>("/me"),
  subscription: () => api<Subscription>("/subscription"),
  servers: () => api<ServersReport>("/servers"),
  config: () => api<NodeConfig>("/config"),
  tariffs: () => api<{ tiers: Tier[] }>("/tariffs"),
  history: () => api<PaymentRow[]>("/payments/history"),
  /** The bot sends its payment menu (tier/period pre-selected) to the user's chat. */
  payInBot: (tier?: string, months?: number) =>
    api<{ sent: boolean }>("/payments/in-bot", {
      method: "POST",
      body: JSON.stringify({ ...(tier ? { tier } : {}), ...(months ? { months } : {}) }),
    }),
  referral: () => api<Referral>("/referral"),
  offer: () => api<{ text: string }>("/legal/offer"),
  privacy: () => api<{ text: string }>("/legal/privacy"),
  accept: () => api<{ accepted: boolean; trial: string }>("/legal/accept", { method: "POST", body: "{}" }),
  logout: () => api<void>("/auth/logout", { method: "POST", body: "{}" }),
};

/** Payment amount for display: XTR is whole Telegram Stars. */
export function formatAmount(amount: number, currency: PaymentRow["currency"]): string {
  if (currency === "RUB") return `${amount} ₽`;
  if (currency === "XTR") return `${amount} ⭐`;
  return `${amount.toFixed(2)} USDT`;
}

/** "12.3 из 200 ГБ" or "1.5 ГБ, без ограничения". */
export function formatTraffic(traffic: TrafficStatus): string {
  const gb = (bytes: number) => {
    const value = bytes / 1024 ** 3;
    return value < 10 ? value.toFixed(1) : value.toFixed(0);
  };
  if (traffic.limit_bytes <= 0) return `${gb(traffic.used_bytes)} ГБ, без ограничения`;
  const base = `${gb(traffic.used_bytes)} из ${gb(traffic.limit_bytes)} ГБ`;
  if (traffic.exhausted) return `${base} — израсходован, ждём обнуления счётчика`;
  return traffic.resets_every_30_days ? `${base} (обнуляется каждые 30 дней)` : base;
}
