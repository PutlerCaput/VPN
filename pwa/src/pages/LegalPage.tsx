import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { endpoints } from "../api/client";
import { Markdown } from "../components/Markdown";

export function LegalPage({ accepted }: { accepted: boolean }) {
  const queryClient = useQueryClient();
  const [adult, setAdult] = useState(false);
  const offer = useQuery({ queryKey: ["offer"], queryFn: endpoints.offer, staleTime: 3_600_000 });
  const [showPrivacy, setShowPrivacy] = useState(false);
  const privacy = useQuery({
    queryKey: ["privacy"],
    queryFn: endpoints.privacy,
    staleTime: 3_600_000,
    enabled: showPrivacy,
  });
  const accept = useMutation({
    mutationFn: endpoints.acceptOffer,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["me"] }),
  });

  return (
    <main className="page">
      <h1>Публичная оферта</h1>
      {!accepted && (
        <div className="card">
          <p>Ключевые условия:</p>
          <ul>
            <li>24 часа бесплатно сразу после принятия, затем от 299 ₽ в месяц;</li>
            <li>отказаться можно в любой момент — вернём деньги за неиспользованные дни;</li>
            <li>сервис только для лиц старше 18 лет;</li>
            <li>сканирование сетей, DDoS, вредоносное ПО и мошенничество запрещены и ведут к прекращению доступа (раздел 4 оферты);</li>
            <li>журнал посещённых ресурсов не ведётся; учётные и платёжные данные передаются органам только по официальному запросу;</li>
            <li>сервис шифрует подключение в публичных сетях и не скрывает личность пользователя.</li>
          </ul>
        </div>
      )}
      <section className="card offer">
        {offer.isLoading && <p className="muted">Загрузка…</p>}
        {offer.isError && <p className="error">Не удалось загрузить текст оферты.</p>}
        {offer.data && <Markdown text={offer.data.text} />}
      </section>
      <section className="card">
        <button type="button" className="btn" onClick={() => setShowPrivacy((v) => !v)}>
          🔒 {showPrivacy ? "Скрыть политику" : "Политика обработки персональных данных"}
        </button>
        {showPrivacy && privacy.isLoading && <p className="muted">Загрузка…</p>}
        {showPrivacy && privacy.isError && <p className="error">Не удалось загрузить политику.</p>}
        {showPrivacy && privacy.data && (
          <div className="offer">
            <Markdown text={privacy.data.text} />
          </div>
        )}
      </section>
      {!accepted && (
        <div className="card sticky">
          <label className="checkbox">
            <input type="checkbox" checked={adult} onChange={(e) => setAdult(e.target.checked)} />
            <span>Мне исполнилось 18 лет, я прочитал(а) оферту и согласен(на) с её условиями, ознакомлен(а) с Политикой обработки персональных данных</span>
          </label>
          <button type="button" className="btn primary" disabled={!adult || accept.isPending} onClick={() => accept.mutate()}>
            {accept.isPending ? "Подключаем…" : "✅ Принимаю"}
          </button>
          {accept.isError && <p className="error">Не удалось сохранить согласие. Попробуйте ещё раз.</p>}
          {accept.data?.trial === "provision_failed" && (
            <p className="error">Оферта принята, но узел пока не готов. Обновите страницу через пару минут.</p>
          )}
          {accept.data?.trial === "trials_paused" && (
            <p className="error">
              Оферта принята. Пробный доступ временно недоступен: сервер близок к месячному лимиту трафика. Подписку
              можно оформить в разделе «Оплата».
            </p>
          )}
        </div>
      )}
    </main>
  );
}
