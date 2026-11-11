// Settings → Notifications: alerts as push notifications on this device. The browser
// asks for permission only after a tap. On an iPhone this works once VibeSynergy is on the
// home screen (iOS 16.4 or later). Which alerts go to push is chosen per kind below.
import { get, post } from "./api.js";
import { esc } from "./format.js";
import { t } from "./i18n.js";

function keyBytes(base64url) {
  const text = atob(base64url.replace(/-/g, "+").replace(/_/g, "/").padEnd(Math.ceil(base64url.length / 4) * 4, "="));
  return Uint8Array.from(text, (c) => c.charCodeAt(0));
}

function supported() {
  return "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;
}

async function current() {
  const registration = await navigator.serviceWorker.getRegistration("/");
  return registration ? { registration, subscription: await registration.pushManager.getSubscription() } : { registration: null, subscription: null };
}

export function pushHtml() {
  return `<section class="card settings-section" data-push aria-labelledby="push-title">
    <h2 id="push-title">${esc(t("Push notifications"))}</h2>
    <p class="sub">${esc(t("Alerts as a notification on this phone or computer, also when VibeSynergy is closed. On an iPhone: add VibeSynergy to the home screen first."))}</p>
    <div class="settings-save" data-push-actions></div>
    <p class="sub" data-push-status role="status" aria-live="polite"></p>
  </section>`;
}

export async function mountPush(root) {
  const host = root?.querySelector("[data-push-actions]");
  if (!host) return;
  const status = root.querySelector("[data-push-status]");
  const say = (text, error = false) => {
    status.textContent = text;
    status.classList.toggle("error-text", error);
  };
  if (!supported()) {
    say(t("This browser can't receive push notifications. On an iPhone, open VibeSynergy from the home screen."));
    return;
  }
  const draw = async () => {
    const { subscription } = await current();
    const on = Boolean(subscription);
    host.innerHTML = on
      ? `<button class="btn" type="button" data-push-test>${esc(t("Send a test notification"))}</button>
         <button class="btn" type="button" data-push-off>${esc(t("Turn off on this device"))}</button>`
      : `<button class="btn primary" type="button" data-push-on>${esc(t("Turn on for this device"))}</button>`;
    if (!status.textContent) say(on ? t("On for this device.") : Notification.permission === "denied" ? t("Notifications are blocked in this browser's settings.") : "");
  };
  host.addEventListener("click", async (event) => {
    const button = event.target.closest("button");
    if (!button) return;
    button.disabled = true;
    try {
      if (button.matches("[data-push-on]")) {
        const permission = await Notification.requestPermission();
        if (permission !== "granted") {
          say(t("Notifications are blocked in this browser's settings."), true);
          return;
        }
        const { data } = await get("push");
        const registration = (await current()).registration || (await navigator.serviceWorker.register("/sw.js", { scope: "/" }));
        await navigator.serviceWorker.ready;
        const subscription = await registration.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: keyBytes(data.key) });
        const json = subscription.toJSON();
        await post("push/subscribe", { endpoint: json.endpoint, p256dh: json.keys.p256dh, auth: json.keys.auth, agent: navigator.userAgent });
        say(t("On for this device."));
      } else if (button.matches("[data-push-off]")) {
        const { subscription } = await current();
        if (subscription) {
          await post("push/unsubscribe", { endpoint: subscription.endpoint });
          await subscription.unsubscribe();
        }
        say(t("Off for this device."));
      } else if (button.matches("[data-push-test]")) {
        say(t("Sending…"));
        await post("push/test");
        say(t("Sent. It should show on this device within a minute."));
      }
    } catch (error) {
      say(t("Not done: {error}", { error: t(error.message) }), true);
    } finally {
      button.disabled = false;
      await draw();
    }
  });
  await draw();
}
