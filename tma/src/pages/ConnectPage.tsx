import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";

import { endpoints, isApiError } from "../api/client";
import { connectUrl } from "../connect";
import { appsForPlatform, detectPlatform } from "../device";
import { QRCode } from "../components/QRCode";
import { ru } from "../i18n/ru";
import { useSession } from "../store/session";
import { haptic, openExternal } from "../telegram";
import ui from "../styles/components.module.css";

export function ConnectPage() {
  const navigate = useSession((s) => s.navigate);
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

  const c = ru.connect;
  const link = config.data?.config_link ?? "";
  const isSub = link.startsWith("https://");
  const configError = isApiError(config.error)
    ? config.error.status === 429
      ? ru.dashboard.rateLimited
      : ru.dashboard.unavailable
    : null;

  async function copy() {
    setCopyFailed(false);
    setCopied(false);
    try {
      await navigator.clipboard.writeText(link);
      haptic("success");
      setCopied(true);
      window.setTimeout(() => setCopied(false), 2000);
    } catch {
      setCopyFailed(true);
    }
  }

  return (
    <main className={ui.page}>
      <button type="button" className={ui.back} onClick={() => navigate("dashboard")}>
        {c.back}
      </button>
      <h1>{c.title}</h1>

      {sub.isLoading && <p className={ui.muted}>{ru.common.loading}</p>}

      {sub.data && !active && (
        <section className={ui.card}>
          {sub.data.status === "suspended" ? (
            <p>{ru.dashboard.suspended}</p>
          ) : (
            <>
              <p>{c.notActive}</p>
              <button type="button" className={`${ui.btn} ${ui.primary}`} onClick={() => navigate("pay")}>
                💳 {ru.dashboard.subscribe}
              </button>
            </>
          )}
        </section>
      )}

      {active && (
        <>
          <p className={`${ui.small} ${ui.muted}`}>{c.device(c.platform[platform])}</p>

          {config.isLoading && (
            <section className={ui.card}>
              <p className={ui.muted}>{ru.common.loading}</p>
            </section>
          )}
          {configError && (
            <section className={ui.card}>
              <p className={ui.error}>{configError}</p>
            </section>
          )}

          {config.data && (
            <>
              {isSub ? (
                <section className={ui.card}>
                  <h2>{c.oneTap}</h2>
                  {apps.map((choice) => {
                    const url = connectUrl(choice.app, link);
                    const install = choice.install;
                    if (!url) return null;
                    return (
                      <div key={choice.app}>
                        <button
                          type="button"
                          className={choice.recommended ? `${ui.btn} ${ui.primary}` : ui.btn}
                          onClick={() => openExternal(url)}
                        >
                          {choice.icon} {c.openIn(choice.name)}
                          {choice.recommended ? ` ${c.recommended}` : ""}
                        </button>
                        {install && (
                          <button type="button" className={ui.install} onClick={() => openExternal(install)}>
                            {c.install(choice.name)}
                          </button>
                        )}
                      </div>
                    );
                  })}
                  <p className={`${ui.small} ${ui.muted}`}>{c.notOpened}</p>
                </section>
              ) : (
                <section className={ui.card}>
                  <h2>{c.scanTitle}</h2>
                  <p className={`${ui.small} ${ui.muted}`}>{c.scanHint(apps.map((a) => a.name).join(", "))}</p>
                  <QRCode value={config.data.qr_payload} />
                  <p className={`${ui.small} ${ui.muted} ${ui.qrCaption}`}>{c.scanLabel}</p>
                  {apps.map((choice) => {
                    const install = choice.install;
                    return install ? (
                      <button key={choice.app} type="button" className={ui.install} onClick={() => openExternal(install)}>
                        {c.installShort(choice.name)}
                      </button>
                    ) : null;
                  })}
                </section>
              )}

              {isSub && (
                <section className={ui.card}>
                  <h2>{c.otherDevice}</h2>
                  <p className={`${ui.small} ${ui.muted}`}>{c.otherHint}</p>
                  <QRCode value={link} />
                  <p className={`${ui.small} ${ui.muted} ${ui.qrCaption}`}>{c.subLabel}</p>
                </section>
              )}

              <section className={ui.card}>
                <button type="button" className={ui.back} onClick={() => setShowLink((v) => !v)}>
                  {showLink ? c.hideLink : c.showLink}
                </button>
                {showLink && (
                  <>
                    <textarea className={ui.mono} readOnly rows={4} value={link} onFocus={(e) => e.target.select()} />
                    <button type="button" className={ui.btn} onClick={copy}>
                      📋 {copied ? ru.common.copied : ru.common.copy}
                    </button>
                    {copyFailed && (
                      <p role="alert" className={ui.error}>
                        {c.copyFailed}
                      </p>
                    )}
                  </>
                )}
                <p className={`${ui.small} ${ui.muted}`}>{c.linkHint}</p>
              </section>
            </>
          )}
        </>
      )}
    </main>
  );
}
