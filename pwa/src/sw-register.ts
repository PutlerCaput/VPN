import { registerSW } from "virtual:pwa-register";

export function registerServiceWorker(): void {
  if (!("serviceWorker" in navigator)) return;
  const updateSW = registerSW({
    immediate: true,
    onNeedRefresh() {
      if (window.confirm("Доступна новая версия приложения. Обновить?")) {
        void updateSW(true);
      }
    },
  });
}
