// Which client app to suggest first for the visitor's device, and where to get
// it. Every app imports a subscription link; the scheme deep links live only in
// connect.ts and the bouncer page /connect.html, which first checks the link is
// this service's own. The install links below are the developers' own pages,
// pinned by tests/test_connect_screen.py so a typo cannot send a buyer of a
// security product to a look-alike.
import type { ConnectApp } from "./connect";

export type Platform = "ios" | "android" | "desktop";

export const PLATFORM_LABEL: Record<Platform, string> = {
  ios: "iPhone или iPad",
  android: "Android",
  desktop: "компьютер",
};

interface AppInfo {
  name: string;
  icon: string;
  /** Platforms where this app is a first choice. */
  best: Platform[];
  /** Every platform it runs on. */
  runs: Platform[];
  /** The developer's own download page, per platform. */
  install: Partial<Record<Platform, string>>;
}

// happ.su routes the visitor to the right store or download itself; Hiddify's
// GitHub releases serve every platform and are reachable where app stores are not.
const HAPP = "https://happ.su";
const HIDDIFY = "https://github.com/hiddify/hiddify-app/releases";

export const APP_INFO: Record<ConnectApp, AppInfo> = {
  happ: {
    name: "Happ",
    icon: "📲",
    best: ["ios", "android"],
    runs: ["ios", "android", "desktop"],
    install: { ios: HAPP, android: HAPP, desktop: HAPP },
  },
  v2raytun: {
    name: "v2rayTUN",
    icon: "📲",
    best: ["android"],
    runs: ["ios", "android"],
    install: {
      ios: "https://apps.apple.com/app/id6476628951",
      android: "https://play.google.com/store/apps/details?id=com.v2raytun.android",
    },
  },
  hiddify: {
    name: "Hiddify",
    icon: "💻",
    best: ["desktop"],
    runs: ["ios", "android", "desktop"],
    install: { ios: HIDDIFY, android: HIDDIFY, desktop: HIDDIFY },
  },
};

/** iPadOS reports a desktop Safari user agent, so touch points tell it apart. */
export function detectPlatform(
  ua: string = navigator.userAgent,
  maxTouchPoints: number = navigator.maxTouchPoints,
): Platform {
  if (/iPhone|iPad|iPod/i.test(ua)) return "ios";
  if (/Macintosh/i.test(ua) && maxTouchPoints > 1) return "ios";
  if (/Android/i.test(ua)) return "android";
  return "desktop";
}

export interface AppChoice {
  app: ConnectApp;
  name: string;
  icon: string;
  recommended: boolean;
  install: string | null;
}

/** The apps that run on this platform, the recommended ones first. */
export function appsForPlatform(platform: Platform): AppChoice[] {
  return (Object.keys(APP_INFO) as ConnectApp[])
    .filter((app) => APP_INFO[app].runs.includes(platform))
    .map((app) => ({
      app,
      name: APP_INFO[app].name,
      icon: APP_INFO[app].icon,
      recommended: APP_INFO[app].best.includes(platform),
      install: APP_INFO[app].install[platform] ?? null,
    }))
    .sort((a, b) => Number(b.recommended) - Number(a.recommended));
}
