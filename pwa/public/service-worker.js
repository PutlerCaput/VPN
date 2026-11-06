/* Web Push handlers, imported into the Workbox-generated sw.js. */
/* global self, clients */

self.addEventListener("push", (event) => {
  let data = { title: "Узел", body: "", url: "/" };
  try {
    if (event.data) {
      data = Object.assign(data, event.data.json());
    }
  } catch (_err) {
    data.body = event.data ? event.data.text() : "";
  }
  const url = typeof data.url === "string" && data.url.startsWith("/") ? data.url : "/";
  event.waitUntil(
    self.registration.showNotification(String(data.title).slice(0, 80), {
      body: String(data.body).slice(0, 240),
      icon: "/icons/icon-192.png",
      badge: "/icons/icon-192.png",
      tag: "node-status",
      renotify: true,
      data: { url },
    }),
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const target = new URL(event.notification.data?.url || "/", self.location.origin).href;
  event.waitUntil(
    clients.matchAll({ type: "window", includeUncontrolled: true }).then((windows) => {
      for (const client of windows) {
        if (client.url.startsWith(self.location.origin) && "focus" in client) {
          client.navigate(target);
          return client.focus();
        }
      }
      return clients.openWindow(target);
    }),
  );
});
