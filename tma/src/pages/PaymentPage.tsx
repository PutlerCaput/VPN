import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { endpoints } from "../api/client";
import { WhyUs } from "../components/WhyUs";
import { ru } from "../i18n/ru";
import { startPayment } from "../payments";
import styles from "../styles/Payment.module.css";
import ui from "../styles/components.module.css";

/** Tariffs come from the API (single source of truth). The bot takes the payment. */
export function PaymentPage() {
  const tariffs = useQuery({ queryKey: ["tariffs"], queryFn: endpoints.tariffs, staleTime: 3_600_000 });
  const tiers = tariffs.data?.tiers ?? [];
  const [busy, setBusy] = useState<string | null>(null);

  const pay = (tier: string, months: number) => {
    if (busy) return;
    setBusy(`${tier}:${months}`);
    void startPayment(tier, months).finally(() => setBusy(null));
  };

  return (
    <main className={ui.page}>
      <h1>{ru.payment.title}</h1>
      <p className={styles.disclaimer}>{ru.payment.disclaimer}</p>
      {tariffs.isError && <p className={ui.small}>{ru.payment.loadFailed}</p>}
      {tiers.map((tier) => (
        <section key={tier.key} className={ui.card}>
          <h2>{tier.title}</h2>
          <p className={styles.disclaimer}>
            {ru.payment.devices(tier.devices)}
            {tier.priority_support ? " · приоритетная поддержка" : ""}
          </p>
          <ul className={styles.features}>
            {tier.features.map((feature) => (
              <li key={feature}>{feature}</li>
            ))}
          </ul>
          {tier.prices.map((price) => (
            <div key={price.months} className={styles.tariff}>
              <span className={styles.term}>
                {ru.payment.months(price.months)}
                {price.save_percent > 0 && <span className={styles.save}> · {ru.payment.save(price.save_percent)}</span>}
              </span>
              <span className={styles.prices}>
                <span className={styles.rub}>{price.rub} ₽</span>
                {price.months > 1 && (
                  <span className={styles.perMonth}>
                    {ru.payment.perMonth(Math.round(price.rub / price.months))}
                  </span>
                )}
              </span>
              <button
                type="button"
                className={`${ui.btn} ${ui.primary}`}
                disabled={busy !== null}
                onClick={() => pay(tier.key, price.months)}
              >
                {busy === `${tier.key}:${price.months}` ? ru.payment.opening : ru.payment.button}
              </button>
            </div>
          ))}
        </section>
      ))}
      <WhyUs />
      <p className={`${ui.small} ${ui.muted}`}>{ru.payment.trial}</p>
    </main>
  );
}
