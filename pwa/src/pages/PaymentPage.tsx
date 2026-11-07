import { useMutation, useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { ApiError, endpoints, type SbpPayment } from "../api/client";

const ERRORS: Record<string, string> = {
  offer_not_accepted: "Сначала примите оферту.",
  too_many_pending: "У вас уже есть неоплаченный счёт. Оплатите его или отмените в боте; неоплаченный счёт отменяется сам через час.",
  ruble_payments_unavailable: "Оплата через ЮKassa временно недоступна.",
  payment_processing: "Заказ уже обрабатывается. Не оплачивайте повторно. Через минуту обновите статус подписки; если он не изменился, обратитесь в поддержку.",
  banned: "Доступ заблокирован.",
  invalid_email: "Проверьте адрес электронной почты.",
  invalid_tier: "Этот тариф недоступен. Выберите другой.",
};

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/;

export function PaymentPage() {
  const [email, setEmail] = useState("");
  const [tierKey, setTierKey] = useState<string | null>(null);
  const [pendingMonths, setPendingMonths] = useState<number | null>(null);
  const tariffs = useQuery({ queryKey: ["tariffs"], queryFn: endpoints.tariffs, staleTime: 3_600_000 });
  const create = useMutation({
    mutationFn: ({ months, tier, mail }: { months: number; tier: string; mail?: string }) =>
      endpoints.createPayment(months, "sbp", tier, mail),
  });
  const tiers = tariffs.data?.tiers ?? [];
  const tier = tiers.find((item) => item.key === tierKey) ?? null;
  const payment = create.data;
  const needsEmail = create.error instanceof ApiError && create.error.message === "receipt_email_required";

  return (
    <main className="page">
      <h1>Оплата аренды</h1>
      <section className="card">
        <p>Оплата через СБП: откроется приложение вашего банка. Карту вводить не нужно.</p>
      </section>

      {tariffs.isError && <p className="error">Не удалось загрузить тарифы. Попробуйте ещё раз.</p>}

      {!payment && (
        <section className="grid">
          {tiers.map((item) => (
            <button
              key={item.key}
              type="button"
              className={item.key === tierKey ? "card tariff selected" : "card tariff"}
              aria-pressed={item.key === tierKey}
              onClick={() => {
                setTierKey(item.key);
                create.reset();
              }}
            >
              <strong>{item.title}</strong>
              <span className="price">от {item.prices[0]?.rub} ₽/мес</span>
              <ul className="small muted">
                {item.features.map((feature) => <li key={feature}>{feature}</li>)}
              </ul>
            </button>
          ))}
        </section>
      )}

      {tier && !payment && (
        <section className="grid">
          {tier.prices.map((price) => (
            <button
              key={price.months}
              type="button"
              className="card tariff"
              disabled={create.isPending}
              onClick={() => {
                setPendingMonths(price.months);
                create.mutate({ months: price.months, tier: tier.key });
              }}
            >
              <strong>{price.months} мес.</strong>
              <span className="price">{price.rub} ₽</span>
              {price.save_percent > 0 && <span className="badge ok">Выгода {price.save_percent}%</span>}
              {price.months > 1 && <span className="small muted">{Math.round(price.rub / price.months)} ₽/мес</span>}
            </button>
          ))}
        </section>
      )}

      {needsEmail && tier && pendingMonths !== null && (
        <form className="card" onSubmit={(event) => {
          event.preventDefault();
          if (EMAIL_RE.test(email.trim()))
            create.mutate({ months: pendingMonths, tier: tier.key, mail: email.trim() });
        }}>
          <label htmlFor="receipt-email">Email для кассового чека (54-ФЗ)</label>
          <input id="receipt-email" type="email" autoComplete="email" maxLength={254}
            value={email} onChange={(event) => setEmail(event.target.value)} required />
          <button type="submit" className="btn primary" disabled={create.isPending || !EMAIL_RE.test(email.trim())}>
            Продолжить
          </button>
        </form>
      )}

      {create.error instanceof ApiError && !needsEmail && (
        <p className="error">{ERRORS[create.error.message] ?? "Не удалось создать платёж."}</p>
      )}
      {payment && "qr_url" in payment && <YooKassaInstructions payment={payment} />}
      {payment && !("qr_url" in payment) && (
        <p className="error">Получен неподдерживаемый способ оплаты. Обратитесь в поддержку.</p>
      )}
    </main>
  );
}

function YooKassaInstructions({ payment }: { payment: SbpPayment }) {
  if (payment.provider !== "yookassa") {
    return <p className="error">Оплата через ЮKassa временно недоступна. Обратитесь в поддержку.</p>;
  }
  return (
    <section className="card">
      <h2>Оплата в рублях: {payment.amount_rub} ₽</h2>
      <a className="btn primary" href={payment.payment_url} target="_blank" rel="noopener noreferrer">
        🏦 Оплатить через СБП
      </a>
      <p>Номер платежа: <code>{payment.payment_id}</code></p>
      <p className="small">
        Оплатите в приложении банка через СБП. Аренда продлится после подтверждения платежа,{" "}
        {payment.receipt === "fiscal"
          ? "кассовый чек придёт на ваш email."
          : "при подключённой интеграции чек самозанятого формируется в «Мой налог» автоматически."}
      </p>
    </section>
  );
}
