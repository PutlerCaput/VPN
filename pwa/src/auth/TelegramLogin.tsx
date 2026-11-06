import { useEffect, useRef, useState } from "react";

import { ApiError, endpoints } from "../api/client";
import { useAppStore } from "../store";

interface TelegramWidgetUser {
  id: number;
  first_name?: string;
  last_name?: string;
  username?: string;
  photo_url?: string;
  auth_date: number;
  hash: string;
}

declare global {
  interface Window {
    onTelegramAuth?: (user: TelegramWidgetUser) => void;
  }
}

const BOT_USERNAME = import.meta.env.VITE_BOT_USERNAME;

export function TelegramLogin() {
  const container = useRef<HTMLDivElement>(null);
  const setTokens = useAppStore((s) => s.setTokens);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    window.onTelegramAuth = (user) => {
      const payload: Record<string, string | number> = {};
      for (const [key, value] of Object.entries(user)) {
        if (value !== undefined && value !== null) payload[key] = value as string | number;
      }
      endpoints
        .loginWithWidget(payload)
        .then((pair) => setTokens(pair.access_token, pair.refresh_token))
        .catch((err: unknown) => {
          setError(err instanceof ApiError && err.status === 429 ? "Слишком много попыток. Подождите минуту." : "Не удалось войти. Попробуйте ещё раз.");
        });
    };
    const script = document.createElement("script");
    script.src = "https://telegram.org/js/telegram-widget.js?22";
    script.async = true;
    script.setAttribute("data-telegram-login", BOT_USERNAME);
    script.setAttribute("data-size", "large");
    script.setAttribute("data-radius", "10");
    script.setAttribute("data-request-access", "write");
    script.setAttribute("data-onauth", "onTelegramAuth(user)");
    const node = container.current;
    node?.appendChild(script);
    return () => {
      delete window.onTelegramAuth;
      if (node) node.replaceChildren();
    };
  }, [setTokens]);

  return (
    <div className="login-widget">
      <div ref={container} />
      {error && <p className="error">{error}</p>}
    </div>
  );
}
