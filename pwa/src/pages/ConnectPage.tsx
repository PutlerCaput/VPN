import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { ApiError, endpoints } from "../api/client";
import { APPS, PLATFORMS, PLATFORM_LABEL, SINGLE_LINK_STEPS, detectPlatform } from "../device";
import type { ClientApp, Platform } from "../device";
import { QRCode } from "../components/QRCode";
import { useAppStore } from "../store";

export function ConnectPage() {
  const navigate = useAppStore((s) => s.navigate);
  const [platform, setPlatform] = useState<Platform>(() => detectPlatform());
  const [chosen, setChosen] = useState<ClientApp | null>(null);
  const [copied, setCopied] = useState(false);
  const [copyFailed, setCopyFailed] = useState(false);
  const [showLink, setShowLink] = useState(false);

  const sub = useQuery({ queryKey: ["subscription"], queryFn: endpoints.subscription });
  const active = sub.data?.status === "active";
  const config = useQuery({
    queryKey: ["config"],
    queryFn: endpoints.config,
    enabled: active,
    staleTime: 60_000,
    retry: false,
  });

  const link = config.data?.config_link ?? "";
  const isSub = link.startsWith("https://");

  async function copy() {
    setCopyFailed(false);
    setCopied(false);
    try {
      await navigator.clipboard.writeText(link);
      setCopied(true);
    } catch {
      setCopyFailed(true);
    }
  }

  function choosePlatform(next: Platform) {
    setPlatform(next);
    setChosen(null);
    setCopied(false);
    setCopyFailed(false);
  }

  // The copy starts inside the tap, which is when browsers allow the clipboard.
  function choose(app: ClientApp) {
    setChosen(app);
    void copy();
  }

  return (
    <main className="page">
      <button type="button" className="btn link" onClick={() => navigate("dashboard")}>
        ‹ Назад
      </button>
      <h1>Подключение</h1>

      {sub.isLoading && <p className="muted">Загрузка…</p>}

      {sub.data && !active && (
        <section className="card">
          {sub.data.status === "suspended" ? (
            <p>Подключение приостановлено из-за аномального трафика. Напишите в поддержку в боте.</p>
          ) : (
            <>
              <p>Подписка не активна. Оплатите доступ — и ссылка для подключения появится здесь.</p>
              <button type="button" className="btn primary block" onClick={() => navigate("pay")}>
                💳 Оплатить
              </button>
            </>
          )}
        </section>
      )}

      {active && (
        <>
          <p className="small muted">
            Выберите устройство и приложение: ссылка скопируется сама, ниже — короткая инструкция.
          </p>
          <div className="chips" role="group" aria-label="Устройство">
            {PLATFORMS.map((p) => (
              <button
                key={p}
                type="button"
                className={p === platform ? "chip active" : "chip"}
                aria-pressed={p === platform}
                onClick={() => choosePlatform(p)}
              >
                {PLATFORM_LABEL[p]}
              </button>
            ))}
          </div>

          {config.isLoading && (
            <section className="card">
              <p className="muted">Получаем ссылку подключения…</p>
            </section>
          )}
          {config.error instanceof ApiError && (
            <section className="card">
              <p className="error">
                {config.error.status === 429 ? "Слишком часто. Попробуйте через минуту." : "Узел временно недоступен."}
              </p>
            </section>
          )}

          {config.data && (
            <>
              <section className="card">
                <h2>Выберите приложение</h2>
                {APPS[platform].map((app) => (
                  <button
                    key={app.key}
                    type="button"
                    className={chosen?.key === app.key ? "btn primary block" : "btn block"}
                    aria-pressed={chosen?.key === app.key}
                    onClick={() => choose(app)}
                  >
                    {app.name}
                  </button>
                ))}
              </section>

              {chosen && (
                <section className="card">
                  <h2>{chosen.name}</h2>
                  <p role="status" className="small">
                    {copied && "✅ Ссылка скопирована."}
                    {copyFailed && "Не удалось скопировать. Нажмите «Показать ссылку вручную» ниже."}
                  </p>
                  <button type="button" className="btn block" onClick={copy}>
                    📋 Скопировать ссылку
                  </button>
                  <ol className="steps">
                    {(isSub ? chosen.steps : [SINGLE_LINK_STEPS]).map((step) => (
                      <li key={step}>{step}</li>
                    ))}
                  </ol>
                  {chosen.note && <p className="small muted">{chosen.note}</p>}
                  <a className="btn primary block" href={chosen.install} target="_blank" rel="noopener noreferrer">
                    Установить {chosen.name}
                  </a>
                  <p className="small muted">Названия пунктов в приложении могут немного отличаться.</p>
                </section>
              )}

              <section className="card">
                <h2>С другого устройства</h2>
                <p className="small muted">Отсканируйте код приложением на другом телефоне или компьютере.</p>
                <QRCode
                  value={isSub ? link : config.data.qr_payload}
                  label={isSub ? "Ссылка-подписка" : "Импорт профиля по QR-коду"}
                />
              </section>

              <section className="card">
                <button type="button" className="btn link" onClick={() => setShowLink((v) => !v)}>
                  {showLink ? "Скрыть ссылку" : "Показать ссылку вручную"}
                </button>
                {showLink && (
                  <>
                    <textarea className="config" readOnly value={link} rows={4} onFocus={(e) => e.target.select()} />
                    <button type="button" className="btn block" onClick={copy}>
                      {copied ? "✅ Скопировано" : "📋 Скопировать"}
                    </button>
                    {copyFailed && (
                      <p role="alert" className="small">
                        Браузер не разрешил копирование. Выделите ссылку выше и скопируйте вручную.
                      </p>
                    )}
                  </>
                )}
                <p className="small muted">Ссылка привязана к вашему аккаунту — не передавайте её третьим лицам.</p>
              </section>
            </>
          )}
        </>
      )}
    </main>
  );
}
