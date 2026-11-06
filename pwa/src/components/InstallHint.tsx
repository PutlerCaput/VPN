import { useEffect, useState } from "react";

interface BeforeInstallPromptEvent extends Event {
  prompt: () => Promise<void>;
  userChoice: Promise<{ outcome: "accepted" | "dismissed" }>;
}

function isStandalone(): boolean {
  return (
    window.matchMedia("(display-mode: standalone)").matches ||
    (navigator as Navigator & { standalone?: boolean }).standalone === true
  );
}

export function InstallHint() {
  const [deferred, setDeferred] = useState<BeforeInstallPromptEvent | null>(null);
  const [hidden, setHidden] = useState(isStandalone());
  const isIos = /iphone|ipad|ipod/i.test(navigator.userAgent);

  useEffect(() => {
    const handler = (event: Event) => {
      event.preventDefault();
      setDeferred(event as BeforeInstallPromptEvent);
    };
    window.addEventListener("beforeinstallprompt", handler);
    return () => window.removeEventListener("beforeinstallprompt", handler);
  }, []);

  if (hidden) return null;
  if (deferred) {
    return (
      <div className="card hint">
        <p>Установите приложение на главный экран — так удобнее проверять статус узла.</p>
        <button
          type="button"
          className="btn"
          onClick={async () => {
            await deferred.prompt();
            await deferred.userChoice;
            setDeferred(null);
            setHidden(true);
          }}
        >
          📲 Установить
        </button>
      </div>
    );
  }
  if (isIos) {
    return (
      <div className="card hint">
        <p>
          Чтобы установить на iPhone (iOS 16.4+): откройте в Safari, нажмите «Поделиться» → «На экран „Домой“». Так же
          включатся уведомления.
        </p>
      </div>
    );
  }
  return null;
}
