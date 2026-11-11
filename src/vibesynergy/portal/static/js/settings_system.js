// Settings → System (split from pages/settings.js): appearance (theme, palette,
// language), region and units, the configuration file and the portal addresses.
// `draw` redraws the whole Settings page after a change.
import { post } from "./api.js";
import { esc } from "./format.js";
import { language, languageChoice, N, t } from "./i18n.js";
import { PALETTES, storedPalette, storedTheme } from "./theme.js";
import { saveRow, select } from "./settings_pieces.js";

export const ZONES = [
  "Europe/Amsterdam",
  "Europe/Brussels",
  "Europe/Berlin",
  "Europe/Paris",
  "Europe/London",
  "Europe/Madrid",
  "UTC",
  "America/New_York",
  "America/Chicago",
  "America/Denver",
  "America/Los_Angeles",
];

export const PALETTE_NAMES = {
  vibesynergy: N("VibeSynergy (standard)"),
  volt: N("Volt"),
  stroom: N("Stroom"),
  hartslag: N("Hartslag"),
};

export function appearanceHtml(prefs) {
  const langChoice = languageChoice() || prefs.language || "auto";
  const themeChoice = storedTheme();
  const paletteChoice = storedPalette() || prefs.palette || "vibesynergy";
  return `<section class="card settings-section" data-section="appearance">
    <h2>${t("Language and theme")}</h2>
    <div class="settings-grid">
      <label>${t("Language")} <select class="btn" name="language" data-autosave-skip>${[["auto", t("Like this browser")], ["en", "English"], ["nl", "Nederlands"]].map(([v, label]) => `<option value="${esc(v)}"${v === langChoice ? " selected" : ""}>${esc(label)}</option>`).join("")}</select></label>
      <label>${t("Colour theme")} <select class="btn" name="palette" data-autosave-skip>${PALETTES.map((v) => `<option value="${esc(v)}"${v === paletteChoice ? " selected" : ""}>${esc(t(PALETTE_NAMES[v]))}</option>`).join("")}</select></label>
      <label>${t("Light or dark")} <select class="btn" name="theme" data-autosave-skip>${[["auto", t("System")], ["light", t("Light")], ["dark", t("Dark")]].map(([v, label]) => `<option value="${esc(v)}"${v === themeChoice ? " selected" : ""}>${esc(label)}</option>`).join("")}</select></label>
    </div>
    <p class="sub">${t("The portal's texts, dates and numbers, and the Dashboard's advice and warnings. Alert emails use the language chosen here (English when it follows the browser).")}</p>
    <p class="sub" data-appearance-status role="status" aria-live="polite"></p>
  </section>
  <section class="card" data-refresh>
    <h2>${t("Page refresh")}</h2>
    <p class="sub">${t("Pages with data reload by themselves while open, so a finished sync shows up. Pages with forms (Settings, Diary, SQL) never do.")}</p>
    <div class="settings-grid"><label>${t("Reload every")} ${select("refresh_min", [["0", t("never")], ["1", t("minute")], ["5", t("5 minutes")], ["15", t("15 minutes")], ["30", t("30 minutes")]], String(prefs.refresh_min ?? 5))}</label></div>
    <div class="settings-save"><span class="sub" data-refresh-status>${t("Saved at once; the same on every device.")}</span></div>
  </section>`;
}

// Time zone and units; the athlete's sport values moved to Training → Zones & thresholds.
export function regionHtml(s) {
  const zones = ZONES.includes(s.user.timezone) ? ZONES : [s.user.timezone, ...ZONES];
  return `<section class="card settings-section" data-section="user">
    <h2>${t("Time zone and units")}</h2>
    <div class="settings-grid">
      <label>${t("Time zone")} ${select("timezone", zones.map((zone) => [zone, zone]), s.user.timezone)}</label>
      <label>${t("Units")} ${select("units", [["metric", t("Metric (km, kg)")], ["imperial", t("Imperial (mi, lb)")]], s.user.units)}</label>
      <label>${t("Temperature")} ${select("temperature", [["c", t("Celsius (°C)")], ["f", t("Fahrenheit (°F)")]], s.user.temperature || "c")}</label>
      <label>${t("Wind")} ${select("wind", [["kmh", "km/h"], ["ms", "m/s"], ["kn", t("Knots (kn)")], ["bft", t("Beaufort (Bft)")]], s.user.wind || "kmh")}</label>
      <label>${t("Training load from")} ${select("load_source", [["intervals", "intervals.icu"], ["vibesynergy", t("VibeSynergy's own calculation")]], s.user.load_source || "intervals")}</label>
    </div>
    <p class="sub">${t("Sex, heart rates and thresholds are under Training.")} <a href="#/zones">${t("Zones & thresholds")}</a>
      · ${t("Notes for your coach and what to remember are on the Coach page.")} <a href="#/coach/memory">${t("Coach settings")}</a></p>
    ${saveRow()}
  </section>`;
}

export function configurationHtml(s) {
  const ro = s.read_only;
  const here = location.hostname.toLowerCase();
  const hosts = s.portal_hosts || [];
  const list = hosts.length
    ? `<ul class="host-list">${hosts.map((host) => `<li><code>${esc(host)}</code>${host === here ? ` <span class="chip">${t("you are here")}</span>` : ""}
        <button class="btn" type="button" data-remove-host="${esc(host)}" aria-label="${esc(t("Remove {host}", { host }))}">${t("Remove")}</button></li>`).join("")}</ul>`
    : `<p class="sub">${t("No extra domains.")}</p>`;
  return `<section class="card" data-configuration>
        <h2>${t("Configuration")}</h2>
        <dl class="settings-system">
          <dt>${t("Public address")}</dt><dd>${ro.public_url ? `<code>${esc(ro.public_url)}/vibesynergy</code><br><span class="sub">${t("For coach apps, webhooks and share links.")}</span>` : esc(t("not set: [server] public_url is empty"))}</dd>
          <dt>${t("MCP endpoint (public)")}</dt><dd><code>${esc(ro.mcp_url || t("not set: [server] public_url is empty"))}</code></dd>
          <dt>${t("Portal (tailnet only)")}</dt><dd><code>${esc(ro.portal_url || t("port {n}", { n: s.portal.port }))}</code></dd>
          <dt>${t("Portal on this server")}</dt><dd><code>${esc(ro.local_url)}</code></dd>
          <dt>${t("Config file")}</dt><dd><code>${esc(s.config_path)}</code></dd>
          <dt>${t("Data folder")}</dt><dd><code>${esc(ro.data_dir)}</code></dd>
        </dl>
        <h3 class="settings-subhead" id="portal-domains">${t("Portal domains")}</h3>
        <p class="sub">${t("Extra names the portal answers on. DNS and the web server in front (Caddy) must also send the name to the portal; VibeSynergy only allows it. A change applies after a restart.")}</p>
        ${list}
        <form class="host-add" data-add-host aria-labelledby="portal-domains">
          <label>${t("Add a domain")} <input class="btn" name="host" autocomplete="off" spellcheck="false" inputmode="url" placeholder="vibesynergy.example.org" required></label>
          <button class="btn" type="submit">${t("Add")}</button>
        </form>
        <p class="sub" data-host-status role="status" aria-live="polite"></p>
        <p class="sub">${t("The public address, the ports and the folders change only in the config file, because a wrong value can cut off this portal or the webhooks.")}</p>
      </section>`;
}

// The server refuses to drop the name this page runs on without a second yes.
export const CURRENT_HOST_WARNING = N("You opened this page on that address. After the restart it will no longer work here.");

export const HOST_MESSAGES = [
  CURRENT_HOST_WARNING,
  N("Enter a domain name, for example vibesynergy.example.org."),
  N("Enter a domain name, not an IP address."),
  N("That is not a valid domain name."),
  N("That address is already allowed."),
  N("That address is not in the list."),
];

export function bindPortalHosts(view, ctx, draw) {
  const root = view.querySelector("[data-configuration]");
  if (!root) return;
  const status = root.querySelector("[data-host-status]");
  const say = (error) => {
    status.classList.add("error-text");
    status.textContent = HOST_MESSAGES.includes(error.message) ? t(error.message) : t("Not saved: {error}", { error: error.message });
  };
  const done = (reply, message) => {
    if (ctx.stale()) return;
    draw(view, { ...ctx, s: reply.data }, message);
  };
  root.querySelector("[data-add-host]")?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const input = event.target.querySelector("[name=host]");
    try {
      const reply = await post("settings/portal_hosts", { action: "add", host: input.value });
      done(reply, t("{host} added. Restart VibeSynergy to use it.", { host: input.value.trim().toLowerCase() }));
    } catch (error) {
      input.setAttribute("aria-invalid", "true");
      say(error);
    }
  });
  root.querySelectorAll("[data-remove-host]").forEach((button) => {
    button.addEventListener("click", async () => {
      const host = button.dataset.removeHost;
      const current = host === location.hostname.toLowerCase();
      const question = current
        ? `${t(CURRENT_HOST_WARNING)} ${t("Remove {host} anyway?", { host })}`
        : t("Remove {host}? The portal stops answering on it after the restart.", { host });
      if (!window.confirm(question)) return;
      const send = (confirm) => post("settings/portal_hosts", { action: "remove", host, confirm });
      try {
        let reply;
        try {
          reply = await send(current);
        } catch (error) {
          if (error.message !== CURRENT_HOST_WARNING || !window.confirm(`${t(CURRENT_HOST_WARNING)} ${t("Remove {host} anyway?", { host })}`)) throw error;
          reply = await send(true);
        }
        done(reply, t("{host} removed. This applies after a restart.", { host }));
      } catch (error) {
        say(error);
      }
    });
  });
}
