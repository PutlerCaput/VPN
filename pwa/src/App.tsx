import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import { ApiError, endpoints, refreshSession } from "./api/client";
import { Nav } from "./components/Nav";
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

  useEffect(() => {
    window.addEventListener("hashchange", syncRoute);
    return () => window.removeEventListener("hashchange", syncRoute);
  }, [syncRoute]);

  useEffect(() => {
    if (booting) void refreshSession().finally(() => setBooting(false));
  }, [booting]);

  const me = useQuery({ queryKey: ["me"], queryFn: endpoints.me, enabled: Boolean(accessToken) });

  useEffect(() => {
    if (me.error instanceof ApiError && me.error.status === 401) clearTokens();
  }, [me.error, clearTokens]);

  if (booting) return <main className="page center muted">Загрузка…</main>;
  if (!accessToken) return <LoginPage />;
  if (me.isLoading || !me.data) {
    return <main className="page center muted">{me.isError ? "Сервис временно недоступен." : "Загрузка…"}</main>;
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
      {route === "pay" && <PaymentPage />}
      {route === "referral" && <ReferralPage />}
      {route === "profile" && <ProfilePage me={me.data} />}
      {route === "legal" && <LegalPage accepted />}
      <Nav />
    </>
  );
}
