import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import { endpoints, formatTraffic, type Status } from "../api/client";
import { BrandHeader } from "../components/Brand";
import { ServersCard } from "../components/ServersCard";
import { formatDate, ru } from "../i18n/ru";
import { startPayment } from "../payments";
import { useSession } from "../store/session";
import styles from "../styles/Dashboard.module.css";
import ui from "../styles/components.module.css";

const TONE: Record<Status, string> = {
  pending: "",
  active: styles.ok ?? "",
  suspended: styles.warn ?? "",
  expired: styles.bad ?? "",
  banned: styles.bad ?? "",
};

function useCountdown(expiresAt: number | null): string | null {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 30_000);
    return () => window.clearInterval(timer);
  }, []);
  if (!expiresAt) return null;
  const left = Math.max(0, expiresAt * 1000 - now);
  const minutes = Math.floor(left / 60_000);
  return ru.dashboard.countdown(Math.floor(minutes / 1440), Math.floor((minutes % 1440) / 60), minutes % 60);
}

export function DashboardPage() {
  const navigate = useSession((s) => s.navigate);
  const queryClient = useQueryClient();
  const [paying, setPaying] = useState(false);
  const sub = useQuery({ queryKey: ["subscription"], queryFn: endpoints.subscription });
  const active = sub.data?.status === "active";
  const countdown = useCountdown(active ? (sub.data?.expires_at ?? null) : null);

  // Returning from the bot after paying: re-fetch once, no polling.
  useEffect(() => {
    const onVisible = () => {
      if (document.visibilityState === "visible") void queryClient.invalidateQueries({ queryKey: ["subscription"] });
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => document.removeEventListener("visibilitychange", onVisible);
  }, [queryClient]);

  return (
    <main className={ui.page}>
      <BrandHeader />
      <h1>{ru.dashboard.title}</h1>
      <p className={`${ui.small} ${ui.muted}`}>{ru.dashboard.tagline}</p>
      <section className={ui.card}>
        {sub.isLoading && <p className={ui.muted}>{ru.common.loading}</p>}
        {sub.isError && <p className={ui.error}>{ru.common.error}</p>}
        {sub.data && (
          <>
            <span className={`${styles.badge} ${TONE[sub.data.status]}`}>{ru.status[sub.data.status]}</span>
            <dl className={styles.facts}>
              <dt>{ru.dashboard.until}</dt>
              <dd>{formatDate(sub.data.expires_at)}</dd>
              {countdown && (
                <>
                  <dt>{ru.dashboard.remaining}</dt>
                  <dd className={styles.countdown}>{countdown}</dd>
                </>
              )}
              {active && sub.data.traffic && (
                <>
                  <dt>{ru.dashboard.traffic}</dt>
                  <dd>{formatTraffic(sub.data.traffic)}</dd>
                </>
              )}
            </dl>
          </>
        )}
        {active && (
          <button type="button" className={`${ui.btn} ${ui.primary} ${ui.cta}`} onClick={() => navigate("connect")}>
            {ru.dashboard.connect}
          </button>
        )}
        <button type="button" className={ui.btn} disabled={sub.isFetching} onClick={() => void sub.refetch()}>
          🔄 {sub.isFetching ? ru.dashboard.checking : ru.dashboard.checkStatus}
        </button>
        <button
          type="button"
          className={`${ui.btn} ${ui.primary}`}
          disabled={paying}
          onClick={() => {
            setPaying(true);
            void startPayment().finally(() => setPaying(false));
          }}
        >
          💳 {paying ? ru.payment.opening : active ? ru.dashboard.renew : ru.dashboard.subscribe}
        </button>
      </section>

      {sub.data?.status === "suspended" && <section className={ui.card}>{ru.dashboard.suspended}</section>}

      <ServersCard />
    </main>
  );
}
