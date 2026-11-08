import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import { ApiError, endpoints, formatDate, formatTraffic, type Me } from "../api/client";
import { CONNECT_APPS, connectUrl } from "../connect";
import { BrandHeader } from "../components/Brand";
import { ServersCard } from "../components/ServersCard";
import { InstallHint } from "../components/InstallHint";
import { QRCode } from "../components/QRCode";
import { useAppStore } from "../store";

const STATUS: Record<Me["status"], { label: string; tone: string }> = {
  pending: { label: "Не активирован", tone: "muted" },
  active: { label: "Активен", tone: "ok" },
  suspended: { label: "Приостановлен", tone: "warn" },
  expired: { label: "Срок истёк", tone: "bad" },
  banned: { label: "Заблокирован", tone: "bad" },
};

export function DashboardPage() {
  const navigate = useAppStore((s) => s.navigate);
  const queryClient = useQueryClient();
  const [showConfig, setShowConfig] = useState(false);
  const [copied, setCopied] = useState(false);
  const [copyFailed, setCopyFailed] = useState(false);
  const sub = useQuery({ queryKey: ["subscription"], queryFn: endpoints.subscription });
  const config = useQuery({
    queryKey: ["config"],
    queryFn: endpoints.config,
    enabled: showConfig && sub.data?.status === "active",
    staleTime: 60_000,
    retry: false,
  });

  useEffect(() => {
    const onVisible = () => {
      if (document.visibilityState === "visible") void queryClient.invalidateQueries({ queryKey: ["subscription"] });
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => document.removeEventListener("visibilitychange", onVisible);
  }, [queryClient]);

  const status = sub.data ? STATUS[sub.data.status] : null;

  return (
    <main className="page">
      <BrandHeader />
      <h1>Будь собой. Безопасно.</h1>
      <p className="small muted">Включи защиту. Отключи угрозы.</p>
      <section className="card">
        {sub.isLoading && <p className="muted">Загрузка…</p>}
        {sub.data && status && (
          <>
            <p>
              <span className={`badge ${status.tone}`}>{status.label}</span>
            </p>
            <dl className="facts">
              <dt>Активен до</dt>
              <dd>{formatDate(sub.data.expires_at)}</dd>
              {sub.data.status === "active" && (
                <>
                  <dt>Осталось дней</dt>
                  <dd>{sub.data.days_remaining}</dd>
                </>
              )}
              {sub.data.status === "active" && sub.data.traffic && (
                <>
                  <dt>Трафик</dt>
                  <dd>{formatTraffic(sub.data.traffic)}</dd>
                </>
              )}
              {sub.data.node_username && (
                <>
                  <dt>Узел</dt>
                  <dd>
                    <code>{sub.data.node_username}</code>
                  </dd>
                </>
              )}
            </dl>
          </>
        )}
        <div className="row">
          <button type="button" className="btn" disabled={sub.isFetching} onClick={() => void sub.refetch()}>
            {sub.isFetching ? "Проверяем…" : "🔄 Проверить статус"}
          </button>
          <button type="button" className="btn primary" onClick={() => navigate("pay")}>
            💳 {sub.data?.status === "active" ? "Продлить" : "Оплатить"}
          </button>
        </div>
      </section>

      {sub.data?.status === "active" && (
        <section className="card">
          <h2>Конфигурация подключения</h2>
          {!showConfig && (
            <button type="button" className="btn" onClick={() => setShowConfig(true)}>
              🔐 Показать конфигурацию
            </button>
          )}
          {showConfig && config.isLoading && <p className="muted">Получаем конфигурацию…</p>}
          {showConfig && config.error instanceof ApiError && (
            <p className="error">{config.error.status === 429 ? "Слишком часто. Попробуйте через минуту." : "Узел временно недоступен."}</p>
          )}
          {config.data && (
            <>
              <QRCode value={config.data.qr_payload} label="Отсканируйте в клиентском приложении" />
              <textarea className="config" readOnly value={config.data.config_link} rows={4} onFocus={(e) => e.target.select()} />
              <button
                type="button"
                className="btn"
                onClick={async () => {
                  setCopyFailed(false);
                  setCopied(false);
                  try {
                    await navigator.clipboard.writeText(config.data.config_link);
                    setCopied(true);
                    window.setTimeout(() => setCopied(false), 2000);
                  } catch {
                    setCopyFailed(true);
                  }
                }}
              >
                {copied ? "✅ Скопировано" : "📋 Скопировать"}
              </button>
              {copyFailed && <p role="alert">Браузер не разрешил копирование. Выделите ссылку в поле выше и скопируйте её вручную.</p>}
              {config.data.config_link.startsWith("https://") && (
                <>
                  <p className="small">Подключить в один клик:</p>
                  {CONNECT_APPS.map(({ app, label }) => {
                    const url = connectUrl(app, config.data.config_link);
                    return url ? (
                      <button key={app} type="button" className="btn" onClick={() => window.open(url, "_blank", "noopener,noreferrer")}>
                        {label}
                      </button>
                    ) : null;
                  })}
                  <p className="small muted">Откроется установленное приложение и добавит профиль. Приложения нет? Скопируйте ссылку выше.</p>
                </>
              )}
              <p className="small muted">
                Клиенты: v2rayNG (Android), Streisand (iPhone), Hiddify (Windows, macOS, Linux).
                Пошаговая инструкция — /connect в боте. Не передавайте конфигурацию
                третьим лицам.
              </p>
            </>
          )}
        </section>
      )}
      {sub.data?.status === "suspended" && (
        <section className="card warn">Подключение приостановлено из-за аномального трафика. Напишите в поддержку в боте.</section>
      )}
      <ServersCard />
      <InstallHint />
    </main>
  );
}
