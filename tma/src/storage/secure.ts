import { getWebApp, type TelegramSecureStorage } from "../telegram";

/**
 * Token storage: Telegram SecureStorage (Bot API 9.0+, Keychain / Keystore).
 * When it is unavailable the values live in memory only and are lost on reload.
 * There is deliberately no fallback to any persistent browser storage.
 */
const memory = new Map<string, string>();

function secureStorage(): TelegramSecureStorage | null {
  const app = getWebApp();
  if (!app || !app.isVersionAtLeast("9.0") || !app.SecureStorage) return null;
  return app.SecureStorage;
}

let unsupported = false;

function withTimeout<T>(executor: (resolve: (value: T) => void) => void, fallback: T): Promise<T> {
  return new Promise((resolve) => {
    const timer = window.setTimeout(() => resolve(fallback), 3000);
    executor((value) => {
      window.clearTimeout(timer);
      resolve(value);
    });
  });
}

export async function secureGet(key: string): Promise<string | null> {
  const storage = unsupported ? null : secureStorage();
  if (!storage) return memory.get(key) ?? null;
  return withTimeout<string | null>((resolve) => {
    storage.getItem(key, (error, value) => {
      if (error) {
        unsupported = true;
        resolve(memory.get(key) ?? null);
        return;
      }
      resolve(value ?? null);
    });
  }, memory.get(key) ?? null);
}

export async function secureSet(key: string, value: string): Promise<void> {
  memory.set(key, value);
  const storage = unsupported ? null : secureStorage();
  if (!storage) return;
  await withTimeout<void>((resolve) => {
    storage.setItem(key, value, (error) => {
      if (error) unsupported = true;
      resolve();
    });
  }, undefined);
}

export async function secureRemove(key: string): Promise<void> {
  memory.delete(key);
  const storage = unsupported ? null : secureStorage();
  if (!storage) return;
  await withTimeout<void>((resolve) => storage.removeItem(key, () => resolve()), undefined);
}

export async function secureClear(): Promise<void> {
  memory.clear();
  const storage = unsupported ? null : secureStorage();
  if (!storage) return;
  await withTimeout<void>((resolve) => storage.clear(() => resolve()), undefined);
}

export function isPersistent(): boolean {
  return !unsupported && secureStorage() !== null;
}

export const KEYS = {
  access: "tma.access",
  refresh: "tma.refresh",
  offerAccepted: "tma.offer_accepted",
} as const;
