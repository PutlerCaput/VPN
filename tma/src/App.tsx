import { useQuery } from "@tanstack/react-query";
import { useCallback, useEffect } from "react";

import { endpoints, isApiError } from "./api/client";
import { bootstrapSession } from "./auth/initData";
import { Splash } from "./components/Brand";
import { Nav } from "./components/Nav";
import { ru } from "./i18n/ru";
import { DashboardPage } from "./pages/DashboardPage";
import { LegalPage } from "./pages/LegalPage";
import { PaymentPage } from "./pages/PaymentPage";
import { ProfilePage } from "./pages/ProfilePage";
import { ReferralPage } from "./pages/ReferralPage";
import { useSession } from "./store/session";
import { closeApp, getWebApp, setBotUsername } from "./telegram";
import ui from "./styles/components.module.css";

export function App() {
  const { auth, route, setAuth } = useSession();

  const boot = useCallback(() => {
    setAuth("booting");
    void bootstrapSession().then(setAuth);
  }, [setAuth]);

  useEffect(() => {
    if (!getWebApp()?.initData) {
      setAuth("outside");
      return;
    }
    boot();
  }, [boot, setAuth]);

  const me = useQuery({ queryKey: ["me"], queryFn: endpoints.me, enabled: auth === "ready" });

  useEffect(() => {
    if (isApiError(me.error) && me.error.status === 401) setAuth("expired");
  }, [me.error, setAuth]);

  useEffect(() => setBotUsername(me.data?.bot_username), [me.data?.bot_username]);

  if (auth === "booting") {
    return (
      <Splash>
        <p className={ui.muted}>{ru.common.loading}</p>
      </Splash>
    );
  }
  if (auth === "outside") {
    return (
      <Splash>
        <p>{ru.session.outside}</p>
      </Splash>
    );
  }
  if (auth === "offline") {
    return (
      <Splash>
        <p>{ru.session.offline}</p>
        <button type="button" className={`${ui.btn} ${ui.primary}`} onClick={boot}>
          {ru.common.retry}
        </button>
      </Splash>
    );
  }
  if (auth === "expired") {
    return (
      <Splash>
        <p>{ru.session.expired}</p>
        <button type="button" className={`${ui.btn} ${ui.primary}`} onClick={closeApp}>
          {ru.session.close}
        </button>
      </Splash>
    );
  }
  if (!me.data) {
    return (
      <Splash>
        <p className={ui.muted}>{me.isError ? ru.common.error : ru.common.loading}</p>
      </Splash>
    );
  }
  if (me.data.status === "banned") {
    return (
      <div className={ui.center}>
        <h1>⛔️ {ru.banned.title}</h1>
        <p>{ru.banned.text}</p>
      </div>
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
