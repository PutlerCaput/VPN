import react from "@vitejs/plugin-react";
import { defineConfig, loadEnv } from "vite";
import { VitePWA } from "vite-plugin-pwa";

// Link previews need the site's absolute address. update_web.sh passes the bot's
// PUBLIC_BASE_URL as PUBLIC_ORIGIN; anything but a plain https origin is ignored.
const DEFAULT_ORIGIN = "https://app.cyberdefency.com";
const ORIGIN_RE = /^https:\/\/[a-z0-9-]+(\.[a-z0-9-]+)+$/;

export default defineConfig(({ mode }) => {
  const requested = loadEnv(mode, ".", "").PUBLIC_ORIGIN ?? "";
  const origin = ORIGIN_RE.test(requested) ? requested : DEFAULT_ORIGIN;
  return {
    plugins: [
      react(),
      {
        name: "public-origin",
        transformIndexHtml: (html: string) => html.replace(/__PUBLIC_ORIGIN__/g, () => origin),
      },
      VitePWA({
        registerType: "prompt",
        injectRegister: null,
        manifest: false,
        filename: "sw.js",
        includeAssets: ["icons/*.png", "brand/*.webp", "fonts/*.woff2", "robots.txt"],
        workbox: {
          importScripts: ["service-worker.js"],
          globPatterns: ["**/*.{js,css,html,svg,png,webp,woff2,webmanifest}"],
          navigateFallback: "/index.html",
          // Server pages the app's shell must not replace: the connect page,
          // the Mini App and the backend.
          navigateFallbackDenylist: [
            /^\/api\//,
            /^\/tma\//,
            /^\/cryptopay\//,
            /^\/sbp\//,
            /^\/healthz/,
            /^\/connect\.html/,
          ],
          cleanupOutdatedCaches: true,
          runtimeCaching: [],
        },
      }),
    ],
    build: {
      target: "es2020",
      sourcemap: false,
      assetsDir: "assets",
    },
    server: {
      port: 5173,
      proxy: {
        "/api": "http://127.0.0.1:8080",
      },
    },
  };
});
