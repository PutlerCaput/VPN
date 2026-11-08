import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import { endpoints, formatTraffic, isApiError, type Status } from "../api/client";
import { BrandHeader } from "../components/Brand";
import { CONNECT_APPS, connectUrl } from "../connect";
import { QRCode } from "../components/QRCode";
import { ServersCard } from "../components/ServersCard";
import { formatDate, ru } from "../i18n/ru";
import { startPayment } from "../payments";
import { haptic, openExternal } from "../telegram";
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
  const queryClient = useQueryClient();
  const [showConfig, setShowConfig] = useState(false);
  const [copied, setCopied] = useState(false);
  const [copyFailed, setCopyFailed] = useState(false);
  const [paying, setPaying] = useState(false);
  const sub = useQuery({ queryKey: ["subscription"], queryFn: endpoints.subscription });
  const active = sub.data?.status === "active";
  const config = useQuery({
    queryKey: ["config"],
    queryFn: endpoints.config,
    enabled: showConfig && active,
    staleTime: 60_000,
    retry: false,
  });
  const countdown = useCountdown(active ? (sub.data?.expires_at ?? null) : null);

  // Returning from the bot after paying: re-fetch once, no polling.
  useEffect(() => {
    const onVisible = () => {
      if (document.visibilityState === "visible") void queryClient.invalidateQueries({ queryKey: ["subscription"] });
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => document.removeEventListener("visibilitychange", onVisible);
  }, [queryClient]);

  const configError = isApiError(config.error)
    ? config.error.status === 429
      ? ru.dashboard.rateLimited
      : ru.dashboard.unavailable
    : null;

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
              {sub.data.status === "active" && sub.data.traffic && (
                <>
                  <dt>{ru.dashboard.traffic}</dt>
                  <dd>{formatTraffic(sub.data.traffic)}</dd>
                </>
              )}
              {sub.data.node_username && (
                <>
                  <dt>{ru.dashboard.node}</dt>
                  <dd>
                    <code>{sub.data.node_username}</code>
                  </dd>
                </>
              )}
            </dl>
          </>
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

      {active && (
        <section className={ui.card}>
          <h2>{ru.dashboard.configTitle}</h2>
          <button type="button" className={ui.btn} onClick={() => setShowConfig((v) => !v)}>
            🔐 {showConfig ? ru.dashboard.hideConfig : ru.dashboard.showConfig}
          </button>
          {showConfig && config.isLoading && <p className={ui.muted}>{ru.common.loading}</p>}
          {showConfig && configError && <p className={ui.error}>{configError}</p>}
          {showConfig && config.data && (
            <>
              <QRCode value={config.data.qr_payload} />
              <textarea className={ui.mono} readOnly rows={4} value={config.data.config_link} onFocus={(e) => e.target.select()} />
              <button
                type="button"
                className={ui.btn}
                onClick={async () => {
                  setCopyFailed(false);
                  setCopied(false);
                  try {
                    await navigator.clipboard.writeText(config.data.config_link);
                    haptic("success");
                    setCopied(true);
                    window.setTimeout(() => setCopied(false), 2000);
                  } catch {
                    setCopyFailed(true);
                  }
                }}
              >
                📋 {copied ? ru.common.copied : ru.common.copy}
              </button>
              {copyFailed && <p role="alert" className={ui.error}>Не удалось скопировать. Выделите ссылку в поле выше и скопируйте её вручную.</p>}
              {config.data.config_link.startsWith("https://") && (
                <>
                  <p className={ui.small}>{ru.dashboard.connectTitle}</p>
                  {CONNECT_APPS.map(({ app, label }) => {
                    const url = connectUrl(app, config.data.config_link);
                    return url ? (
                      <button key={app} type="button" className={ui.btn} onClick={() => openExternal(url)}>
                        {label}
                      </button>
                    ) : null;
                  })}
                  <p className={`${ui.small} ${ui.muted}`}>{ru.dashboard.connectHint}</p>
                </>
              )}
              <p className={`${ui.small} ${ui.muted}`}>{ru.dashboard.configHint}</p>
            </>
          )}
        </section>
      )}
    </main>
  );
}
