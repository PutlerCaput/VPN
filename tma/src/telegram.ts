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

const BOT_NAME = /^[A-Za-z0-9_]{5,32}$/;

// The server reports the bot's name in /me; the build-time value is only a fallback.
let botUsername: string = import.meta.env.VITE_BOT_USERNAME ?? "";

export function setBotUsername(name: string | null | undefined): void {
  if (name && BOT_NAME.test(name)) botUsername = name;
}

/** Opens the bot chat, optionally with a /start parameter. False when the name is unknown. */
export function openBotChat(start?: string): boolean {
  if (!BOT_NAME.test(botUsername)) return false;
  const url = `https://t.me/${botUsername}${start ? `?start=${encodeURIComponent(start)}` : ""}`;
  const app = getWebApp();
  if (app) app.openTelegramLink(url);
  else window.open(url, "_blank", "noopener,noreferrer");
  return true;
}

export function notify(message: string): void {
  const app = getWebApp();
  if (app) app.showAlert(message);
  else window.alert(message);
}

export function closeApp(): void {
  getWebApp()?.close();
}

export function haptic(type: "error" | "success" | "warning"): void {
  getWebApp()?.HapticFeedback?.notificationOccurred(type);
}
