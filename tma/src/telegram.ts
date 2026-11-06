/** Typed access to the official telegram-web-app.js (window.Telegram.WebApp). */

type StorageCallback<T> = (error: string | null, result?: T) => void;

export interface TelegramSecureStorage {
  setItem(key: string, value: string, callback?: StorageCallback<boolean>): void;
  getItem(key: string, callback: (error: string | null, value?: string | null, canRestore?: boolean) => void): void;
  removeItem(key: string, callback?: StorageCallback<boolean>): void;
  clear(callback?: StorageCallback<boolean>): void;
}

export interface TelegramWebApp {
  initData: string;
  initDataUnsafe: { user?: { id: number; first_name?: string; username?: string }; start_param?: string };
  version: string;
  platform: string;
  colorScheme: "light" | "dark";
  ready(): void;
  expand(): void;
  close(): void;
  isVersionAtLeast(version: string): boolean;
  disableVerticalSwipes?: () => void;
  openTelegramLink(url: string): void;
  openLink(url: string, options?: { try_instant_view?: boolean }): void;
  showAlert(message: string, callback?: () => void): void;
  onEvent(event: string, handler: () => void): void;
  offEvent(event: string, handler: () => void): void;
  HapticFeedback?: { notificationOccurred(type: "error" | "success" | "warning"): void };
  SecureStorage?: TelegramSecureStorage;
}

declare global {
  interface Window {
    Telegram?: { WebApp?: TelegramWebApp };
  }
}

export function getWebApp(): TelegramWebApp | null {
  const app = window.Telegram?.WebApp;
  return app && typeof app.ready === "function" ? app : null;
}

export function initWebApp(): TelegramWebApp | null {
  const app = getWebApp();
  if (!app) return null;
  app.ready();
  app.expand();
  if (app.isVersionAtLeast("7.7") && typeof app.disableVerticalSwipes === "function") {
    app.disableVerticalSwipes();
  }
  return app;
}

const BOT_USERNAME = import.meta.env.VITE_BOT_USERNAME;

/** Payments live in the bot: the Mini App only opens the bot chat. */
export function openBotPayment(tier?: string, months?: number): void {
  // Deep link formats the bot understands: pay, pay_<tier>, pay_<tier>_<months>.
  const suffix = tier && months ? `pay_${tier}_${months}` : tier ? `pay_${tier}` : "pay";
  const url = `https://t.me/${BOT_USERNAME}?start=${suffix}`;
  const app = getWebApp();
  if (app) app.openTelegramLink(url);
  else window.open(url, "_blank", "noopener,noreferrer");
}

export function closeApp(): void {
  getWebApp()?.close();
}

export function haptic(type: "error" | "success" | "warning"): void {
  getWebApp()?.HapticFeedback?.notificationOccurred(type);
}
