import { getWebApp } from "../telegram";
import { KEYS, secureGet, secureRemove, secureSet } from "../storage/secure";

const API_BASE = import.meta.env.VITE_API_BASE ?? "/api/v1";

interface TokenPair {
  access_token: string;
  refresh_token: string;
  expires_in: number;
}

let accessToken: string | null = null;
let inflight: Promise<boolean> | null = null;

export function getAccessToken(): string | null {
  return accessToken;
}

async function store(pair: TokenPair): Promise<void> {
  accessToken = pair.access_token;
  await secureSet(KEYS.access, pair.access_token);
  await secureSet(KEYS.refresh, pair.refresh_token);
}

async function post(path: string, body: unknown): Promise<TokenPair | null> {
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
    if (!response.ok) return null;
    return (await response.json()) as TokenPair;
  } catch {
    return null;
  } finally {
    window.clearTimeout(timer);
  }
}

/** Rotates the refresh token. Single-flight so parallel 401s refresh once. */
export function refresh(): Promise<boolean> {
  if (inflight) return inflight;
  inflight = (async () => {
    try {
      const token = await secureGet(KEYS.refresh);
      if (!token) return false;
      const pair = await post("/auth/refresh", { refresh_token: token });
      if (!pair) {
        await secureRemove(KEYS.refresh);
        await secureRemove(KEYS.access);
        accessToken = null;
        return false;
      }
      await store(pair);
      return true;
    } finally {
      inflight = null;
    }
  })();
  return inflight;
}

let loginOnce: Promise<boolean> | null = null;

/**
 * Sends the raw initData string to the backend for HMAC validation. It is never
 * parsed client-side for authorization. Each initData is single-use server-side
 * (replay guard), so the call is memoised for the lifetime of the page.
 */
function loginWithInitData(): Promise<boolean> {
  if (loginOnce) return loginOnce;
  loginOnce = (async () => {
    const initData = getWebApp()?.initData;
    if (!initData) return false;
    const pair = await post("/auth/telegram", { initData });
    if (!pair) return false;
    await store(pair);
    return true;
  })();
  return loginOnce;
}

/** Boot: a fresh initData login first (valid ≤ 5 min after launch), then refresh. */
export async function bootstrapSession(): Promise<boolean> {
  if (await loginWithInitData()) return true;
  const stored = await secureGet(KEYS.access);
  if (stored) accessToken = stored;
  return refresh();
}

export async function clearSession(): Promise<void> {
  accessToken = null;
  await secureRemove(KEYS.access);
  await secureRemove(KEYS.refresh);
  await secureRemove(KEYS.offerAccepted);
}
