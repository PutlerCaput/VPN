import { ru } from "../i18n/ru";
import styles from "../styles/Payment.module.css";
import ui from "../styles/components.module.css";

/** Facts about this service next to what to check at any provider. Names no competitor. */
export function WhyUs() {
  return (
    <section className={ui.card} aria-labelledby="why-title">
      <h2 id="why-title">{ru.why.title}</h2>
      <p className={styles.disclaimer}>{ru.why.intro}</p>
      {ru.why.rows.map((row) => (
        <div key={row.topic} className={styles.why}>
          <strong>{row.topic}</strong>
          <p>
            <span className={styles.whyLabel}>{ru.why.colWe}:</span> {row.we}
          </p>
          <p className={styles.disclaimer}>
            <span className={styles.whyLabel}>{ru.why.colCheck}:</span> {row.check}
          </p>
        </div>
      ))}
    </section>
  );
}
