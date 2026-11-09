import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";

import { ApiError, endpoints } from "../api/client";
import { connectUrl } from "../connect";
import { appsForPlatform, detectPlatform, PLATFORM_LABEL } from "../device";
import { QRCode } from "../components/QRCode";
import { useAppStore } from "../store";

export function ConnectPage() {
  const navigate = useAppStore((s) => s.navigate);
  const platform = useMemo(() => detectPlatform(), []);
  const apps = useMemo(() => appsForPlatform(platform), [platform]);
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
      window.setTimeout(() => setCopied(false), 2000);
    } catch {
      setCopyFailed(true);
    }
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
            Ваше устройство: {PLATFORM_LABEL[platform]}. Нажмите приложение — профиль добавится сам.
          </p>

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
              {isSub ? (
                <section className="card">
                  <h2>Подключиться в один тап</h2>
                  {apps.map((choice) => {
                    const url = connectUrl(choice.app, link);
                    if (!url) return null;
                    return (
                      <div key={choice.app} className="approw">
                        <button
                          type="button"
                          className={choice.recommended ? "btn primary block" : "btn block"}
                          onClick={() => window.open(url, "_blank", "noopener,noreferrer")}
                        >
                          {choice.icon} Открыть в {choice.name}
                          {choice.recommended ? " — советуем" : ""}
                        </button>
                        {choice.install && (
                          <a className="small muted install" href={choice.install} target="_blank" rel="noopener noreferrer">
                            Нет приложения? Установить {choice.name}
                          </a>
                        )}
                      </div>
                    );
                  })}
                  <p className="small muted">
                    Ничего не открылось — сначала установите приложение по ссылке под кнопкой, затем нажмите её снова. Если приложения нет в вашем магазине, напишите в поддержку бота.
                  </p>
                </section>
              ) : (
                <section className="card">
                  <h2>Отсканируйте в приложении</h2>
                  <p className="small muted">
                    Откройте приложение ({apps.map((c) => c.name).join(", ")}), нажмите «+» и отсканируйте код.
                  </p>
                  <QRCode value={config.data.qr_payload} label="Импорт профиля по QR-коду" />
                  {apps.map((choice) =>
                    choice.install ? (
                      <a key={choice.app} className="small muted install" href={choice.install} target="_blank" rel="noopener noreferrer">
                        Установить {choice.name}
                      </a>
                    ) : null,
                  )}
                </section>
              )}

              {isSub && (
                <section className="card">
                  <h2>С другого устройства</h2>
                  <p className="small muted">Отсканируйте код приложением на другом телефоне или компьютере.</p>
                  <QRCode value={link} label="Ссылка-подписка" />
                </section>
              )}

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
