import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import { ApiError, endpoints, refreshSession } from "./api/client";
import { Splash } from "./components/Brand";
import { Nav } from "./components/Nav";
import { ConnectPage } from "./pages/ConnectPage";
import { DashboardPage } from "./pages/DashboardPage";
import { LegalPage } from "./pages/LegalPage";
import { LoginPage } from "./pages/LoginPage";
import { PaymentPage } from "./pages/PaymentPage";
import { ProfilePage } from "./pages/ProfilePage";
import { ReferralPage } from "./pages/ReferralPage";
import { useAppStore } from "./store";

export function App() {
  const { route, accessToken, refreshToken, syncRoute, clearTokens } = useAppStore();
  const [booting, setBooting] = useState(Boolean(refreshToken && !accessToken));
  const [offline, setOffline] = useState(false);

  useEffect(() => {
    window.addEventListener("hashchange", syncRoute);
    return () => window.removeEventListener("hashchange", syncRoute);
  }, [syncRoute]);

  useEffect(() => {
    if (!booting) return;
    void refreshSession()
      .then((result) => setOffline(result === "unavailable"))
      .finally(() => setBooting(false));
  }, [booting]);

  const me = useQuery({ queryKey: ["me"], queryFn: endpoints.me, enabled: Boolean(accessToken) });

  useEffect(() => {
    if (me.error instanceof ApiError && me.error.status === 401) clearTokens();
  }, [me.error, clearTokens]);

  if (booting) {
    return (
      <Splash>
        <p className="muted">Загрузка…</p>
      </Splash>
    );
  }
  if (offline && refreshToken && !accessToken) {
    return (
      <Splash>
        <p>Нет связи с сервером. Проверьте интернет и попробуйте ещё раз.</p>
        <button
          type="button"
          className="btn primary"
          onClick={() => {
            setOffline(false);
            setBooting(true);
          }}
        >
          Повторить
        </button>
      </Splash>
    );
  }
  if (!accessToken) return <LoginPage />;
  if (me.isLoading || !me.data) {
    return (
      <Splash>
        <p className="muted">{me.isError ? "Сервис временно недоступен." : "Загрузка…"}</p>
      </Splash>
    );
  }
  if (me.data.status === "banned") {
    return (
      <main className="page center">
        <h1>⛔️ Доступ заблокирован</h1>
        <p>Доступ прекращён автоматически за нарушение правил использования (раздел 4 оферты).</p>
      </main>
    );
  }
  if (!me.data.offer_accepted) return <LegalPage accepted={false} />;

  return (
    <>
      {route === "dashboard" && <DashboardPage />}
      {route === "connect" && <ConnectPage />}
      {route === "pay" && <PaymentPage />}
      {route === "referral" && <ReferralPage />}
      {route === "profile" && <ProfilePage me={me.data} />}
      {route === "legal" && <LegalPage accepted />}
      <Nav />
    </>
  );
}
