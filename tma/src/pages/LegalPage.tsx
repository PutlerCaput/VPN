import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { endpoints } from "../api/client";
import { Markdown } from "../components/Markdown";
import { ru } from "../i18n/ru";
import { KEYS, secureSet } from "../storage/secure";
import { useSession } from "../store/session";
import { haptic } from "../telegram";
import styles from "../styles/Legal.module.css";
import ui from "../styles/components.module.css";

export function LegalPage({ accepted }: { accepted: boolean }) {
  const queryClient = useQueryClient();
  const navigate = useSession((s) => s.navigate);
  const [adult, setAdult] = useState(false);
  const [showPrivacy, setShowPrivacy] = useState(false);
  const offer = useQuery({ queryKey: ["offer"], queryFn: endpoints.offer, staleTime: Infinity });
  const privacy = useQuery({
    queryKey: ["privacy"],
    queryFn: endpoints.privacy,
    staleTime: Infinity,
    enabled: showPrivacy,
  });
  const accept = useMutation({
    mutationFn: endpoints.accept,
    onSuccess: async () => {
      haptic("success");
      await secureSet(KEYS.offerAccepted, "1");
      await queryClient.invalidateQueries({ queryKey: ["me"] });
    },
    onError: () => haptic("error"),
  });

  return (
    <main className={ui.page}>
      <h1>{ru.legal.title}</h1>
      {!accepted && (
        <section className={ui.card}>
          <h2>{ru.legal.summaryTitle}</h2>
          <ul>
            {ru.legal.summary.map((item) => (
              <li key={item}>{item}</li>
            ))}
          </ul>
        </section>
      )}
      <section className={`${ui.card} ${styles.offer}`}>
        {offer.isLoading && <p className={ui.muted}>{ru.common.loading}</p>}
        {offer.data && <Markdown text={offer.data.text} />}
      </section>
      <section className={ui.card}>
        <button type="button" className={ui.btn} onClick={() => setShowPrivacy((v) => !v)}>
          🔒 {showPrivacy ? ru.legal.privacyHide : ru.legal.privacyShow}
        </button>
        {showPrivacy && privacy.isLoading && <p className={ui.muted}>{ru.common.loading}</p>}
        {showPrivacy && privacy.isError && <p className={ui.error}>{ru.common.error}</p>}
        {showPrivacy && privacy.data && (
          <div className={styles.offer}>
            <Markdown text={privacy.data.text} />
          </div>
        )}
      </section>
      {accepted ? (
        <button type="button" className={ui.btn} onClick={() => navigate("profile")}>
          {ru.legal.back}
        </button>
      ) : (
        <section className={`${ui.card} ${styles.sticky}`}>
          <label className={styles.checkbox}>
            <input type="checkbox" checked={adult} onChange={(e) => setAdult(e.target.checked)} />
            <span>{ru.legal.adult}</span>
          </label>
          <button
            type="button"
            className={`${ui.btn} ${ui.primary}`}
            disabled={!adult || accept.isPending}
            onClick={() => accept.mutate()}
          >
            ✅ {accept.isPending ? ru.legal.accepting : ru.legal.accept}
          </button>
          {accept.isError && <p className={ui.error}>{ru.legal.failed}</p>}
          {accept.data?.trial === "provision_failed" && <p className={ui.error}>{ru.legal.provisionFailed}</p>}
        </section>
      )}
    </main>
  );
}
