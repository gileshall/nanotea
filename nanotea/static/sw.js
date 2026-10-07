// Service worker: shows pushed messages as notifications, tells open pages to update, and opens them when tapped.

// A new version takes over at once rather than waiting for every page to close.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));

const UPDATE_WAIT_MS = 3000;

self.addEventListener("push", (event) => {
  const data = event.data ? event.data.json() : {};
  event.waitUntil((async () => {
    // Open pages update first, so the notification never announces what the page doesn't show yet. A frozen
    // page can't answer, and the notification can't wait on it past UPDATE_WAIT_MS.
    const windows = await clients.matchAll({ type: "window", includeUncontrolled: true });
    await Promise.race([
      Promise.all(windows.map((w) => new Promise((resolve) => {
        const channel = new MessageChannel();
        channel.port1.onmessage = resolve;
        w.postMessage({ type: "update", url: data.url || "/" }, [channel.port2]);
      }))),
      new Promise((resolve) => setTimeout(resolve, UPDATE_WAIT_MS)),
    ]);
    // iOS revokes push permission from apps that receive a push without showing one.
    await self.registration.showNotification(data.title || "New message", {
      body: data.body || "",
      tag: data.tag,
      data: { url: data.url || "/" },
    });
    if ("setAppBadge" in self.navigator && typeof data.unseen === "number") {
      try {
        await self.navigator.setAppBadge(data.unseen);
      } catch (err) {
        console.error("setAppBadge failed", err);
      }
    }
  })());
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const url = new URL(event.notification.data.url, self.location.origin).href;
  event.waitUntil((async () => {
    const windows = await clients.matchAll({ type: "window", includeUncontrolled: true });
    if (windows.length) {
      // The page decides: it won't navigate away from a recording or a half-typed message.
      windows[0].postMessage({ type: "open", url });
      return windows[0].focus();
    }
    return clients.openWindow(url);
  })());
});
