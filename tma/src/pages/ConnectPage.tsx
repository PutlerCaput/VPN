import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { endpoints, isApiError } from "../api/client";
import { APPS, AUTO_DIRECT_APPS, PLATFORMS, detectPlatform } from "../device";
import type { ClientApp, Platform } from "../device";
import { QRCode } from "../components/QRCode";
import { ru } from "../i18n/ru";
import { useSession } from "../store/session";
import { haptic, openExternal } from "../telegram";
import ui from "../styles/components.module.css";

export function ConnectPage() {
  const navigate = useSession((s) => s.navigate);
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

  const c = ru.connect;
  const link = config.data?.config_link ?? "";
  const isSub = link.startsWith("https://");
  const configError = isApiError(config.error)
    ? config.error.status === 429
      ? ru.dashboard.rateLimited
      : ru.dashboard.unavailable
    : null;
  const guide = chosen ? c.guides[platform]?.[chosen.key] : undefined;
  const steps = guide ? (isSub ? guide.steps : [c.singleLink]) : [];
  // This app gets "Russian sites directly" with the subscription itself.
  const autoDirect =
    isSub && config.data?.ru_direct_auto === true && chosen !== null && AUTO_DIRECT_APPS.includes(chosen.key);

  async function copy() {
    setCopyFailed(false);
    setCopied(false);
    try {
      await navigator.clipboard.writeText(link);
      haptic("success");
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

  // The copy starts inside the tap, which is when the clipboard is allowed.
  function choose(app: ClientApp) {
    setChosen(app);
    void copy();
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
          <p className={`${ui.small} ${ui.muted}`}>{c.intro}</p>
          <div className={ui.chips} role="group">
            {PLATFORMS.map((p) => (
              <button
                key={p}
                type="button"
                className={p === platform ? `${ui.chip} ${ui.chipActive}` : ui.chip}
                aria-pressed={p === platform}
                onClick={() => choosePlatform(p)}
              >
                {c.platform[p]}
              </button>
            ))}
          </div>

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
              <section className={ui.card}>
                <h2>{c.pickApp}</h2>
                {APPS[platform].map((app) => (
                  <button
                    key={app.key}
                    type="button"
                    className={chosen?.key === app.key ? `${ui.btn} ${ui.primary}` : ui.btn}
                    aria-pressed={chosen?.key === app.key}
                    onClick={() => choose(app)}
                  >
                    {app.name}
                  </button>
                ))}
              </section>

              {chosen && guide && (
                <section className={ui.card}>
                  <h2>{chosen.name}</h2>
                  <p role="status" className={ui.small}>
                    {copied && c.copied}
                    {copyFailed && c.copyFailed}
                  </p>
                  <button type="button" className={ui.btn} onClick={copy}>
                    📋 {c.copyLink}
                  </button>
                  <ol className={ui.steps}>
                    {steps.map((step) => (
                      <li key={step}>{step}</li>
                    ))}
                  </ol>
                  {guide.note && <p className={`${ui.small} ${ui.muted}`}>{guide.note}</p>}
                  {autoDirect && <p className={ui.small}>{c.autoDirectNote}</p>}
                  <button
                    type="button"
                    className={`${ui.btn} ${ui.primary}`}
                    onClick={() => openExternal(chosen.install)}
                  >
                    {c.install(chosen.name)}
                  </button>
                  <p className={`${ui.small} ${ui.muted}`}>{c.labelsDiffer}</p>
                </section>
              )}

              {chosen && guide && (
                <section className={ui.card}>
                  <details className={ui.direct}>
                    <summary>{c.directTitle}</summary>
                    <p className={ui.small}>{c.directIntro}</p>
                    <ol className={ui.steps}>
                      {(autoDirect ? c.autoDirectSteps : guide.direct).map((step) => (
                        <li key={step}>{step}</li>
                      ))}
                    </ol>
                    <p className={`${ui.small} ${ui.muted}`}>{c.directCheck}</p>
                    <p className={`${ui.small} ${ui.muted}`}>{c.directOwn}</p>
                  </details>
                </section>
              )}

              <section className={ui.card}>
                <h2>{c.otherDevice}</h2>
                <p className={`${ui.small} ${ui.muted}`}>{c.otherHint}</p>
                <QRCode value={isSub ? link : config.data.qr_payload} />
                <p className={`${ui.small} ${ui.muted} ${ui.qrCaption}`}>{isSub ? c.subLabel : c.scanLabel}</p>
              </section>

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
                        {c.manualCopyFailed}
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
