// One-tap links to the connect page on this site. The page opens the chosen VPN
// app with the subscription link, which travels in the URL fragment only.
export const CONNECT_APPS = [
  { app: "happ", label: "📲 Happ" },
  { app: "v2raytun", label: "📲 v2rayTUN" },
  { app: "hiddify", label: "💻 Hiddify" },
] as const;

export type ConnectApp = (typeof CONNECT_APPS)[number]["app"];

/** Connect-page address for a subscription link, or null for any other link. */
export function connectUrl(app: ConnectApp, link: string): string | null {
  if (!link.startsWith("https://")) return null;
  const url = new URL("/connect.html", window.location.origin);
  url.hash = `app=${app}&sub=${encodeURIComponent(link)}`;
  return url.toString();
}
