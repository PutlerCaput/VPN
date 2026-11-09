import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect } from "react";

import { endpoints, formatDate, formatTraffic, type Me } from "../api/client";
import { BrandHeader } from "../components/Brand";
import { ServersCard } from "../components/ServersCard";
import { InstallHint } from "../components/InstallHint";
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
  const sub = useQuery({ queryKey: ["subscription"], queryFn: endpoints.subscription });

  useEffect(() => {
    const onVisible = () => {
      if (document.visibilityState === "visible") void queryClient.invalidateQueries({ queryKey: ["subscription"] });
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => document.removeEventListener("visibilitychange", onVisible);
  }, [queryClient]);

  const status = sub.data ? STATUS[sub.data.status] : null;
  const active = sub.data?.status === "active";

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
              {active && (
                <>
                  <dt>Осталось дней</dt>
                  <dd>{sub.data.days_remaining}</dd>
                </>
              )}
              {active && sub.data.traffic && (
                <>
                  <dt>Трафик</dt>
                  <dd>{formatTraffic(sub.data.traffic)}</dd>
                </>
              )}
            </dl>
          </>
        )}
        {active && (
          <button type="button" className="btn primary block cta" onClick={() => navigate("connect")}>
            🚀 Подключиться
          </button>
        )}
        <div className="row">
          <button type="button" className="btn" disabled={sub.isFetching} onClick={() => void sub.refetch()}>
            {sub.isFetching ? "Проверяем…" : "🔄 Проверить статус"}
          </button>
          <button type="button" className="btn primary" onClick={() => navigate("pay")}>
            💳 {active ? "Продлить" : "Оплатить"}
          </button>
        </div>
      </section>

      {sub.data?.status === "suspended" && (
        <section className="card warn">Подключение приостановлено из-за аномального трафика. Напишите в поддержку в боте.</section>
      )}
      <ServersCard />
      <InstallHint />
    </main>
  );
}
