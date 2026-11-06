import { useQuery } from "@tanstack/react-query";

import { endpoints } from "../api/client";
import { ru } from "../i18n/ru";
import { type Route, useSession } from "../store/session";
import styles from "../styles/components.module.css";

const ITEMS: { route: Route; icon: string }[] = [
  { route: "dashboard", icon: "🔐" },
  { route: "pay", icon: "💳" },
  { route: "referral", icon: "🎁" },
  { route: "profile", icon: "👤" },
];

export function Nav() {
  const me = useQuery({ queryKey: ["me"], queryFn: endpoints.me });
  const { route, navigate } = useSession();
  return (
    <nav className={styles.nav}>
      {ITEMS.filter((item) => item.route !== "referral" || me.data?.referral_enabled === true).map((item) => (
        <button
          key={item.route}
          type="button"
          className={route === item.route ? `${styles.tab} ${styles.active}` : styles.tab}
          onClick={() => navigate(item.route)}
        >
          <span aria-hidden="true">{item.icon}</span>
          <span>{ru.nav[item.route as keyof typeof ru.nav]}</span>
        </button>
      ))}
    </nav>
  );
}
