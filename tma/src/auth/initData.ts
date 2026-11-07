import { getWebApp } from "../telegram";
import { KEYS, secureGet, secureRemove, secureSet } from "../storage/secure";

const API_BASE = import.meta.env.VITE_API_BASE ?? "/api/v1";

interface TokenPair {
  access_token: string;
  refresh_token: string;
  expires_in: number;
}

/**
 * "rejected": the server refused the credentials, the session is over.
 * "unavailable": no network, a timeout, a 5xx while the bot restarts or a rate
 * limit. The session may be fine, so nothing is deleted.
 */
export type AuthResult = "ok" | "rejected" | "unavailable";
export type BootResult = "ready" | "expired" | "offline";

let accessToken: string | null = null;
let inflight: Promise<AuthResult> | null = null;

export function getAccessToken(): string | null {
  return accessToken;
}

async function store(pair: TokenPair): Promise<void> {
  accessToken = pair.access_token;
  await secureSet(KEYS.access, pair.access_token);
  await secureSet(KEYS.refresh, pair.refresh_token);
}

async function post(path: string, body: unknown): Promise<TokenPair | "rejected" | "unavailable"> {
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), 15_000);
  try {
    const response = await fetch(`${API_BASE}${path}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      credentials: "omit",
      signal: controller.signal,
    });
    if (response.ok) return (await response.json()) as TokenPair;
    return response.status >= 500 || response.status === 429 ? "unavailable" : "rejected";
  } catch {
    return "unavailable";
  } finally {
    window.clearTimeout(timer);
  }
}

/** Rotates the refresh token. Single-flight so parallel 401s refresh once. */
export function refresh(): Promise<AuthResult> {
  if (inflight) return inflight;
  inflight = (async (): Promise<AuthResult> => {
    try {
      const token = await secureGet(KEYS.refresh);
      if (!token) return "rejected";
      const result = await post("/auth/refresh", { refresh_token: token });
      if (result === "unavailable") return "unavailable";
      if (result === "rejected") {
        await secureRemove(KEYS.refresh);
        await secureRemove(KEYS.access);
        accessToken = null;
        return "rejected";
      }
      await store(result);
      return "ok";
    } finally {
      inflight = null;
    }
  })();
  return inflight;
}

let loginOnce: Promise<AuthResult> | null = null;

/**
 * Sends the raw initData string to the backend for HMAC validation. It is never
 * parsed client-side for authorization. Each initData is single-use server-side
 * (replay guard), so the call is memoised for the lifetime of the page, except
 * after an unavailable attempt: that one may be retried.
 */
function loginWithInitData(): Promise<AuthResult> {
  if (loginOnce) return loginOnce;
  const attempt = (async (): Promise<AuthResult> => {
    const initData = getWebApp()?.initData;
    if (!initData) return "rejected";
    const result = await post("/auth/telegram", { initData });
    if (result === "unavailable" || result === "rejected") return result;
    await store(result);
    return "ok";
  })();
  loginOnce = attempt;
  void attempt.then((result) => {
    if (result === "unavailable" && loginOnce === attempt) loginOnce = null;
  });
  return attempt;
}

/** Boot: a fresh initData login first (valid ≤ 5 min after launch), then refresh. */
export async function bootstrapSession(): Promise<BootResult> {
  const login = await loginWithInitData();
  if (login === "ok") return "ready";
  const stored = await secureGet(KEYS.access);
  if (stored) accessToken = stored;
  const refreshed = await refresh();
  if (refreshed === "ok") return "ready";
  return login === "unavailable" || refreshed === "unavailable" ? "offline" : "expired";
}

export async function clearSession(): Promise<void> {
  accessToken = null;
  await secureRemove(KEYS.access);
  await secureRemove(KEYS.refresh);
  await secureRemove(KEYS.offerAccepted);
}
