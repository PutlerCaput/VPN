import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";
import { VitePWA } from "vite-plugin-pwa";

export default defineConfig({
  base: "/tma/",
  plugins: [
    react(),
    VitePWA({
      registerType: "autoUpdate",
      injectRegister: null,
      manifest: false,
      filename: "service-worker.js",
      scope: "/tma/",
      includeAssets: ["icons/*.png", "robots.txt"],
      workbox: {
        globPatterns: ["**/*.{js,css,svg,png,webmanifest}"],
        navigateFallback: null,
        cleanupOutdatedCaches: true,
        clientsClaim: true,
        skipWaiting: true,
        runtimeCaching: [],
      },
    }),
  ],
  build: {
    target: "es2020",
    sourcemap: false,
    assetsDir: "assets",
    chunkSizeWarningLimit: 250,
  },
  server: {
    port: 5174,
    proxy: {
      "/api": "http://127.0.0.1:8080",
    },
  },
});
