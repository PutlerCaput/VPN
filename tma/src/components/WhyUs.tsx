import { ru } from "../i18n/ru";
import styles from "../styles/Payment.module.css";
import ui from "../styles/components.module.css";

/** Verifiable facts about this service. Names no competitor and makes no claim we cannot check. */
export function WhyUs() {
  return (
    <section className={ui.card} aria-labelledby="why-title">
      <h2 id="why-title">{ru.why.title}</h2>
      <p className={styles.disclaimer}>{ru.why.intro}</p>
      {ru.why.rows.map((row) => (
        <div key={row.topic} className={styles.why}>
          <strong>{row.topic}</strong>
          <p>{row.text}</p>
        </div>
      ))}
    </section>
  );
}
