import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import { endpoints, formatAmount, formatDate, type Me } from "../api/client";
import { currentSubscription, disablePush, enablePush, pushSupported } from "../push";
import { useAppStore } from "../store";

const PAYMENT_STATUS = { pending: "ожидает", paid: "оплачен", cancelled: "отменён", refunded: "возвращён" } as const;

export function ProfilePage({ me }: { me: Me }) {
  const queryClient = useQueryClient();
  const { refreshToken, clearTokens, navigate } = useAppStore();
  const history = useQuery({ queryKey: ["history"], queryFn: endpoints.history });
  const [pushOn, setPushOn] = useState(false);
  const [pushBusy, setPushBusy] = useState(false);

  useEffect(() => {
    void currentSubscription().then((s) => setPushOn(Boolean(s)));
  }, []);

  const logout = async () => {
    await endpoints.logout(refreshToken).catch(() => undefined);
    clearTokens();
    queryClient.clear();
  };

  return (
    <main className="page">
      <h1>Профиль</h1>
      <section className="card">
        <dl className="facts">
          <dt>Telegram ID</dt>
          <dd>
            <code>{me.telegram_id}</code>
          </dd>
          <dt>Username</dt>
          <dd>{me.username ? `@${me.username}` : "—"}</dd>
          <dt>Приглашено</dt>
          <dd>{me.referral_count}</dd>
        </dl>
      </section>

      {pushSupported() && (
        <section className="card">
          <h2>Уведомления</h2>
          <p className="small muted">Напомним об окончании аренды и сообщим о зачислении оплаты.</p>
          <button
            type="button"
            className="btn"
            disabled={pushBusy}
            onClick={async () => {
              setPushBusy(true);
              try {
                if (pushOn) {
                  await disablePush();
                  setPushOn(false);
                } else {
                  setPushOn(await enablePush());
                }
              } finally {
                setPushBusy(false);
              }
            }}
          >
            {pushOn ? "🔕 Выключить" : "🔔 Включить"}
          </button>
        </section>
      )}

      <section className="card">
        <h2>История платежей</h2>
        {history.data && history.data.length === 0 && <p className="muted">Платежей пока нет.</p>}
        {history.data && history.data.length > 0 && (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Дата</th>
                  <th>Срок</th>
                  <th>Сумма</th>
                  <th>Статус</th>
                </tr>
              </thead>
              <tbody>
                {history.data.map((p) => (
                  <tr key={p.payment_id}>
                    <td>{formatDate(p.created_at)}</td>
                    <td>{p.months} мес.</td>
                    <td>
                      {formatAmount(p.amount, p.currency)}
                    </td>
                    <td>{PAYMENT_STATUS[p.status]}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section className="card">
        <button type="button" className="btn link" onClick={() => navigate("legal")}>
          📄 Публичная оферта
        </button>
        <button type="button" className="btn danger" onClick={() => void logout()}>
          Выйти
        </button>
      </section>
    </main>
  );
}
