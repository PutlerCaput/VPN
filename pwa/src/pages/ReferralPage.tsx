import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { endpoints } from "../api/client";

const SHARE_TEXT = "Шифрую подключение в публичных Wi-Fi через свой технический узел. Попробуй сутки бесплатно:";

export function ReferralPage() {
  const referral = useQuery({ queryKey: ["referral"], queryFn: endpoints.referral });
  const [copied, setCopied] = useState(false);
  const data = referral.data;

  return (
    <main className="page">
      <h1>{data?.enabled ? "Пригласите друзей" : "Реферальные бонусы"}</h1>
      {data && (
        <>
          {data.enabled && data.link ? <section className="card">
            <p>
              Когда друг впервые оплатит аренду, вы получите <strong>+{data.inviter_bonus_days} дней</strong>, а он —{" "}
              <strong>+{data.invitee_bonus_days} дня</strong>.
            </p>
            <input className="config" readOnly value={data.link} onFocus={(e) => e.target.select()} />
            <div className="row">
              <button
                type="button"
                className="btn"
                onClick={async () => {
                  await navigator.clipboard.writeText(data.link);
                  setCopied(true);
                  window.setTimeout(() => setCopied(false), 2000);
                }}
              >
                {copied ? "✅ Скопировано" : "📋 Скопировать"}
              </button>
              <a
                className="btn primary"
                href={`https://t.me/share/url?url=${encodeURIComponent(data.link)}&text=${encodeURIComponent(SHARE_TEXT)}`}
                target="_blank"
                rel="noopener noreferrer"
              >
                📤 Поделиться
              </a>
            </div>
          </section> : <section className="card">
            <p>Новые приглашения отключены. Ранее начисленные дни сохраняются.</p>
          </section>}
          <section className="card stats">
            <div>
              <span className="big">{data.invited_count}</span>
              <span className="muted">приглашено</span>
            </div>
            <div>
              <span className="big">{data.bonus_days}</span>
              <span className="muted">бонусных дней</span>
            </div>
          </section>
        </>
      )}
    </main>
  );
}
