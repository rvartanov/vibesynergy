// Settings → System → Best source per measurement. Per metric: which sources deliver it,
// the order in use (best first), which source the value comes from now, and buttons to move a
// source up or down. A move saves at once; the next data update (derive) applies it.
import { get, post } from "./api.js";
import { esc, metricLabel, sourceName, userDate } from "./format.js";
import { N, t } from "./i18n.js";
import { isoToday } from "./period_picker.js";

const GROUP_NAMES = {
  sleep: N("Sleep"),
  hrv: N("HRV"),
  body: N("Body"),
  heart: N("Heart"),
  activity: N("Activity"),
  other: N("Other measurements"),
};

const domId = (metric) => `bs-${metric.replace(/[^a-z0-9]/g, "-")}`;

export async function bestSourceSettings(el, { stale, catalogue = {} }) {
  let data = (await get("settings/best_source")).data;
  if (stale() || !el.isConnected) return;

  function nowText(m) {
    if (!m.now) return t("No value yet");
    const source = sourceName(m.now.source);
    return m.now.date === isoToday()
      ? t("Today from {source}", { source })
      : t("Last value ({date}) from {source}", { date: userDate(m.now.date), source });
  }

  function sourceRow(m, src, index, label) {
    const name = sourceName(src.id);
    const last = index === m.sources.length - 1;
    return `<li class="bs-source">
      <span class="bs-rank" aria-hidden="true">${index + 1}</span>
      <span class="bs-name">${esc(name)}<span class="sub">${esc(t("{n} days, last {date}", { n: src.days, date: userDate(src.last) }))}</span></span>
      <span class="bs-moves">
        <button class="btn" type="button" data-move="up" data-source="${esc(src.id)}"${index === 0 ? " disabled" : ""}
          aria-label="${esc(t("Move {source} up for {metric}", { source: name, metric: label }))}">${esc(t("Up"))}</button>
        <button class="btn" type="button" data-move="down" data-source="${esc(src.id)}"${last ? " disabled" : ""}
          aria-label="${esc(t("Move {source} down for {metric}", { source: name, metric: label }))}">${esc(t("Down"))}</button>
      </span>
    </li>`;
  }

  function metricItem(m) {
    const label = metricLabel(m.metric, catalogue);
    const id = domId(m.metric);
    return `<li class="bs-metric" data-metric="${esc(m.metric)}">
      <div class="bs-head">
        <h4 id="${id}" tabindex="-1">${esc(label)}</h4>
        <span class="chip">${esc(m.custom ? t("Your order") : t("Default"))}</span>
        <span class="sub">${esc(nowText(m))}</span>
      </div>
      <ol class="bs-order" aria-labelledby="${id}">${m.sources.map((src, i) => sourceRow(m, src, i, label)).join("")}</ol>
      <div class="bs-foot">
        ${m.custom ? `<button class="btn" type="button" data-bs-reset aria-label="${esc(t("Reset {metric} to default", { metric: label }))}">${esc(t("Reset to default"))}</button>` : ""}
        <span class="sub" data-bs-status role="status" aria-live="polite"></span>
      </div>
    </li>`;
  }

  function draw() {
    const choice = data.metrics.filter((m) => m.sources.length > 1);
    const single = data.metrics.filter((m) => m.sources.length === 1);
    const groups = data.groups
      .map((gid) => [gid, choice.filter((m) => m.group === gid)])
      .filter(([, rows]) => rows.length);
    const config = Object.entries(data.config_rules || {});
    // A group you opened stays open when this redraws after a change.
    const open = new Set([...el.querySelectorAll("details[data-bs-group][open]")].map((d) => d.dataset.bsGroup));
    // Settings → System shows this inside a disclosure with the same title.
    el.innerHTML = `<h2 id="best-source-title" class="visually-hidden">${esc(t("Best source per measurement"))}</h2>
      <p class="sub">${esc(t("When several sources measure the same thing, the top one counts. A change is saved at once and applies at the next data update."))}</p>
      ${groups.length ? groups.map(([gid, rows]) => `<details class="collapsible settings-inline-more" data-bs-group="${esc(gid)}"${open.has(gid) ? " open" : ""}><summary>${esc(t("{group} ({n})", { group: t(GROUP_NAMES[gid]), n: rows.length }))}</summary>
        <ul class="bs-list">${rows.map(metricItem).join("")}</ul></details>`).join("")
        : `<p>${esc(t("Every measurement comes from one source, so there is nothing to choose."))}</p>`}
      ${single.length ? `<details class="collapsible settings-inline-more"><summary>${esc(t("Measurements from one source ({n})", { n: single.length }))}</summary>
        <ul class="bs-single">${single.map((m) => `<li>${esc(metricLabel(m.metric, catalogue))}: ${esc(sourceName(m.sources[0].id))}</li>`).join("")}</ul></details>` : ""}
      ${config.length ? `<details class="collapsible settings-inline-more"><summary>${esc(t("Older rules in the config file"))}</summary>
        <p class="sub">${esc(t("These rules cover a whole group and take effect after a restart. An order chosen above comes first."))}</p>
        <ul>${config.map(([prefix, order]) => `<li><code>${esc(prefix || "*")}</code>: ${esc(order.map(sourceName).join(", "))}</li>`).join("")}</ul>
        <button class="btn" type="button" data-bs-config-reset>${esc(t("Remove these rules"))}</button>
        <span class="sub" data-bs-config-status role="status" aria-live="polite"></span></details>` : ""}`;
    el.setAttribute("aria-labelledby", "best-source-title");
  }

  async function save(metric, order, focus) {
    const item = el.querySelector(`[data-metric="${CSS.escape(metric)}"]`);
    const status = item?.querySelector("[data-bs-status]");
    if (status) {
      status.classList.remove("error-text");
      status.textContent = t("Saving…");
    }
    try {
      data = (await post("settings/best_source/metric", { metric, order })).data;
    } catch (error) {
      if (status) {
        status.classList.add("error-text");
        status.textContent = t("Not saved: {error}", { error: error.message });
      }
      return;
    }
    if (stale() || !el.isConnected) return;
    draw();
    const again = el.querySelector(`[data-metric="${CSS.escape(metric)}"]`);
    if (!again) return;
    again.querySelector("[data-bs-status]").textContent = t("Saved. Applies at the next data update.");
    focus(again);
  }

  el.addEventListener("click", async (event) => {
    const button = event.target.closest("button");
    if (!button || !el.contains(button)) return;
    if (button.hasAttribute("data-bs-config-reset")) {
      const status = el.querySelector("[data-bs-config-status]");
      try {
        await post("settings/best_source/reset", {});
        status.textContent = t("Removed. This applies after a restart.");
      } catch (error) {
        status.textContent = t("Not saved: {error}", { error: error.message });
      }
      return;
    }
    const item = button.closest("[data-metric]");
    if (!item) return;
    const metric = item.dataset.metric;
    const m = data.metrics.find((row) => row.metric === metric);
    if (button.hasAttribute("data-bs-reset")) {
      await save(metric, null, (again) => again.querySelector("h4").focus());
      return;
    }
    const source = button.dataset.source;
    const dir = button.dataset.move;
    const order = [...m.order];
    const at = order.indexOf(source);
    const to = dir === "up" ? at - 1 : at + 1;
    if (at < 0 || to < 0 || to >= order.length) return;
    [order[at], order[to]] = [order[to], order[at]];
    // Keep the keyboard on the source that moved; at the top or bottom, on its other button.
    await save(metric, order, (again) => {
      const pick = (d) => again.querySelector(`[data-source="${CSS.escape(source)}"][data-move="${d}"]`);
      const target = pick(dir);
      (target && !target.disabled ? target : pick(dir === "up" ? "down" : "up"))?.focus();
    });
  });

  draw();
}
