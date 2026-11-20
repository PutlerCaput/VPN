import { useQuery } from "@tanstack/react-query";

import { endpoints } from "../api/client";
import { ru } from "../i18n/ru";
import ui from "../styles/components.module.css";

/** Locations and their state, shown like the location list of popular client apps. */
export function ServersCard() {
  const servers = useQuery({ queryKey: ["servers"], queryFn: endpoints.servers, staleTime: 120_000 });
  const data = servers.data;
  if (!data) return null;
  return (
    <section className={ui.card}>
      <h2>{ru.servers.title}</h2>
      {!data.panel_ok && <p className={ui.muted}>{ru.servers.unknown}</p>}
      {data.panel_ok && (
        <ul>
          {data.locations.map((location) => (
            <li key={location.name}>
              {location.online ? "🟢" : "🔴"} {location.name} — {location.online ? ru.servers.online : ru.servers.offline}
            </li>
          ))}
        </ul>
      )}
      <p className={`${ui.small} ${ui.muted}`}>{ru.servers.panelScope}</p>
      {data.subscription && data.locations.length > 1 && (
        <p className={`${ui.small} ${ui.muted}`}>{ru.servers.pickInClient}</p>
      )}
    </section>
  );
}
