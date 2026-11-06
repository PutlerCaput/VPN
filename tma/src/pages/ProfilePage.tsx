import { useQuery, useQueryClient } from "@tanstack/react-query";

import { endpoints, formatAmount, type Me } from "../api/client";
import { clearSession } from "../auth/initData";
import { formatDate, ru } from "../i18n/ru";
import { secureClear } from "../storage/secure";
import { useSession } from "../store/session";
import { closeApp } from "../telegram";
import styles from "../styles/Profile.module.css";
import ui from "../styles/components.module.css";

export function ProfilePage({ me }: { me: Me }) {
  const queryClient = useQueryClient();
  const navigate = useSession((s) => s.navigate);
  const history = useQuery({ queryKey: ["history"], queryFn: endpoints.history });

  const logout = async () => {
    await endpoints.logout().catch(() => undefined);
    await clearSession();
    await secureClear();
    queryClient.clear();
    closeApp();
  };

  return (
    <main className={ui.page}>
      <h1>{ru.profile.title}</h1>
      <section className={ui.card}>
        <dl className={styles.facts}>
          <dt>{ru.profile.id}</dt>
          <dd>
            <code>{me.telegram_id}</code>
          </dd>
          <dt>{ru.profile.username}</dt>
          <dd>{me.username ? `@${me.username}` : "—"}</dd>
        </dl>
      </section>
      <section className={ui.card}>
        <h2>{ru.profile.history}</h2>
        {history.isLoading && <p className={ui.muted}>{ru.common.loading}</p>}
        {history.data?.length === 0 && <p className={ui.muted}>{ru.profile.empty}</p>}
        {history.data && history.data.length > 0 && (
          <div className={styles.tableWrap}>
            <table className={styles.table}>
              <thead>
                <tr>
                  <th>{ru.profile.date}</th>
                  <th>{ru.profile.term}</th>
                  <th>{ru.profile.amount}</th>
                  <th>{ru.profile.state}</th>
                </tr>
              </thead>
              <tbody>
                {history.data.map((p) => (
                  <tr key={p.payment_id}>
                    <td>{formatDate(p.created_at)}</td>
                    <td>{ru.payment.months(p.months)}</td>
                    <td>{formatAmount(p.amount, p.currency)}</td>
                    <td>{ru.profile.paymentStatus[p.status]}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
      <section className={ui.card}>
        <button type="button" className={ui.btn} onClick={() => navigate("legal")}>
          📄 {ru.profile.offer}
        </button>
        <button type="button" className={`${ui.btn} ${ui.danger}`} onClick={() => void logout()}>
          {ru.profile.logout}
        </button>
      </section>
    </main>
  );
}
