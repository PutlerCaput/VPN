import { useQuery } from "@tanstack/react-query";

import { endpoints } from "../api/client";
import { type Route, useAppStore } from "../store";

const ITEMS: { route: Route; label: string; icon: string }[] = [
  { route: "dashboard", label: "Узел", icon: "🔐" },
  { route: "pay", label: "Оплата", icon: "💳" },
  { route: "referral", label: "Друзья", icon: "🎁" },
  { route: "profile", label: "Профиль", icon: "👤" },
];

export function Nav() {
  const me = useQuery({ queryKey: ["me"], queryFn: endpoints.me });
  const route = useAppStore((s) => s.route);
  const navigate = useAppStore((s) => s.navigate);
  return (
    <nav className="tabbar" aria-label="Навигация">
      {ITEMS.filter((item) => item.route !== "referral" || me.data?.referral_enabled === true).map((item) => (
        <button
          key={item.route}
          type="button"
          className={route === item.route ? "tab active" : "tab"}
          onClick={() => navigate(item.route)}
          aria-current={route === item.route ? "page" : undefined}
        >
          <span aria-hidden="true">{item.icon}</span>
          <span>{item.label}</span>
        </button>
      ))}
    </nav>
  );
}
