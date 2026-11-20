import { useQuery } from "@tanstack/react-query";

import { endpoints } from "../api/client";

/** Locations and their state, shown like the location list of popular client apps. */
export function ServersCard() {
  const servers = useQuery({ queryKey: ["servers"], queryFn: endpoints.servers, staleTime: 120_000 });
  const data = servers.data;
  if (!data) return null;
  return (
    <section className="card">
      <h2>Серверы</h2>
      {!data.panel_ok && <p className="muted">Сейчас не удалось проверить состояние серверов.</p>}
      {data.panel_ok && (
        <ul className="servers">
          {data.locations.map((location) => (
            <li key={location.name}>
              {location.online ? "🟢" : "🔴"} {location.name} — {location.online ? "запущен в панели" : "сбой в панели"}
            </li>
          ))}
        </ul>
      )}
      <p className="small muted">Статус по данным панели; подключение из России не проверено.</p>
      {data.subscription && data.locations.length > 1 && (
        <p className="small muted">Доступные маршруты смотрите в приложении подключения. Автопереключение зависит от клиента.</p>
      )}
    </section>
  );
}
