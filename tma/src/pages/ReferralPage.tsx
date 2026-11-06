import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { endpoints } from "../api/client";
import { ru } from "../i18n/ru";
import { getWebApp, haptic } from "../telegram";
import styles from "../styles/Referral.module.css";
import ui from "../styles/components.module.css";

export function ReferralPage() {
  const referral = useQuery({ queryKey: ["referral"], queryFn: endpoints.referral });
  const [copied, setCopied] = useState(false);
  const data = referral.data;

  const share = () => {
    if (!data?.enabled || !data.link) return;
    const url = `https://t.me/share/url?url=${encodeURIComponent(data.link)}&text=${encodeURIComponent(ru.referral.shareText)}`;
    const app = getWebApp();
    if (app) app.openTelegramLink(url);
    else window.open(url, "_blank", "noopener,noreferrer");
  };

  return (
    <main className={ui.page}>
      <h1>{data?.enabled ? ru.referral.title : "Реферальные бонусы"}</h1>
      {referral.isLoading && <p className={ui.muted}>{ru.common.loading}</p>}
      {data && (
        <>
          {data.enabled && data.link ? <section className={ui.card}>
            <p>{ru.referral.text(data.inviter_bonus_days)}</p>
            <input className={ui.mono} readOnly value={data.link} onFocus={(e) => e.target.select()} />
            <button
              type="button"
              className={ui.btn}
              onClick={async () => {
                await navigator.clipboard.writeText(data.link);
                haptic("success");
                setCopied(true);
                window.setTimeout(() => setCopied(false), 2000);
              }}
            >
              📋 {copied ? ru.common.copied : ru.common.copy}
            </button>
            <button type="button" className={`${ui.btn} ${ui.primary}`} onClick={share}>
              📤 {ru.referral.share}
            </button>
          </section> : <section className={ui.card}>
            <p>Новые приглашения отключены. Ранее начисленные дни сохраняются.</p>
          </section>}
          <section className={`${ui.card} ${styles.stats}`}>
            <div>
              <span className={styles.big}>{data.invited_count}</span>
              <span className={ui.muted}>{ru.referral.invited}</span>
            </div>
            <div>
              <span className={styles.big}>{data.bonus_days}</span>
              <span className={ui.muted}>{ru.referral.bonus}</span>
            </div>
          </section>
        </>
      )}
    </main>
  );
}
