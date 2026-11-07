import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { endpoints, type Tier, type TierPrice } from "../api/client";
import { WhyUs } from "../components/WhyUs";
import { ru } from "../i18n/ru";
import { startPayment } from "../payments";
import styles from "../styles/Payment.module.css";
import ui from "../styles/components.module.css";

/** The period with the largest saving; none when no period is discounted. */
function bestMonths(tier: Tier): number | null {
  const best = tier.prices.reduce<TierPrice | null>(
    (top, price) => (price.save_percent > (top?.save_percent ?? 0) ? price : top),
    null,
  );
  return best?.months ?? null;
}

/** Tariffs come from the API (single source of truth). The bot takes the payment. */
export function PaymentPage() {
  const tariffs = useQuery({ queryKey: ["tariffs"], queryFn: endpoints.tariffs, staleTime: 3_600_000 });
  const tiers = tariffs.data?.tiers ?? [];
  const [busy, setBusy] = useState(false);
  // Selected period per tier; the best-value period is preselected.
  const [picked, setPicked] = useState<Record<string, number>>({});

  const pay = (tier: string, months: number) => {
    if (busy) return;
    setBusy(true);
    void startPayment(tier, months).finally(() => setBusy(false));
  };

  return (
    <main className={ui.page}>
      <h1>{ru.payment.title}</h1>
      <p className={styles.disclaimer}>{ru.payment.disclaimer}</p>
      {tariffs.isError && <p className={ui.small}>{ru.payment.loadFailed}</p>}
      {tiers.map((tier) => {
        const best = bestMonths(tier);
        const months = picked[tier.key] ?? best ?? tier.prices[0]?.months;
        const chosen = tier.prices.find((price) => price.months === months);
        return (
          <section key={tier.key} className={ui.card}>
            <h2>{tier.title}</h2>
            <ul className={styles.features}>
              {tier.features.map((feature) => (
                <li key={feature}>{feature}</li>
              ))}
            </ul>
            <div role="radiogroup" aria-label={ru.payment.period} className={styles.periods}>
              {tier.prices.map((price) => {
                const active = price.months === months;
                return (
                  <button
                    key={price.months}
                    type="button"
                    role="radio"
                    aria-checked={active}
                    className={active ? `${styles.period} ${styles.periodActive}` : styles.period}
                    onClick={() => setPicked((prev) => ({ ...prev, [tier.key]: price.months }))}
                  >
                    <span className={styles.term}>
                      {price.months === best && <span className={styles.bestBadge}>🔥 {ru.payment.best}</span>}
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
                  </button>
                );
              })}
            </div>
            {chosen && (
              <button
                type="button"
                className={`${ui.btn} ${ui.primary} ${styles.cta}`}
                disabled={busy}
                onClick={() => pay(tier.key, chosen.months)}
              >
                {busy ? ru.payment.opening : ru.payment.payFor(chosen.rub, chosen.months)}
              </button>
            )}
            <p className={`${ui.small} ${ui.muted}`}>{ru.payment.refund}</p>
          </section>
        );
      })}
      <WhyUs />
      <p className={`${ui.small} ${ui.muted}`}>{ru.payment.trial}</p>
    </main>
  );
}
