// Diary: the morning check-in, notes, and the injury log with a body map. Everything here is
// entered by you; the warnings on injuries are fixed rules, not a diagnosis.
import { get, post } from "../api.js";
import { alpha, mount, series, token, tooltipBase } from "../charts.js";
import { esc, hoursMinutes, km, number, shortDate, sourceName, sportName, tagName } from "../format.js";
import { tagChip } from "../symbols.js";
import { N, t } from "../i18n.js";
import {
  apiParams,
  bindPeriodPicker,
  mergePeriodValue,
  periodPickerHtml,
  saveStored,
  writePeriodQuery,
} from "../period_picker.js";
import { noteActivityHtml } from "./day.js";

const STORAGE = "vibesynergy.period.diary";
const DEFAULT = { period: "last_1m" };
const EXTRA = [["all", N("All time")]];
const CHECKIN_LABELS = {
  sleep_quality: [N("Sleep quality"), N("poor"), N("good")],
  soreness: [N("Muscle soreness"), N("fresh"), N("heavy")],
  mood: [N("Mood"), N("sad"), N("cheerful")],
  stress: [N("Stress"), N("calm"), N("stressed")],
  motivation: [N("Motivation"), N("low"), N("high")],
};
const TIMING_LABELS = { during: N("During training"), after: N("After training"), morning: N("In the morning"), rest: N("At rest") };
const STATUS_CHIP = { active: "critical", improving: "warning", resolved: "good" };
const FLAG_CLASS = { stop: "danger", warn: "warning", info: "info" };

// Body map: [x, y] per part on a 120 × 250 figure. Front view: the person's right is on the
// viewer's left. Back view: the person's left is on the viewer's left. `null` side = centre.
const FRONT = {
  head: [[60, 16]],
  neck: [[60, 34]],
  shoulder: [[36, 48, "right"], [84, 48, "left"]],
  chest: [[60, 58]],
  elbow: [[28, 84, "right"], [92, 84, "left"]],
  wrist: [[23, 108, "right"], [97, 108, "left"]],
  hand: [[20, 121, "right"], [100, 121, "left"]],
  abdomen: [[60, 88]],
  hip: [[45, 110, "right"], [75, 110, "left"]],
  groin: [[54, 122, "right"], [66, 122, "left"]],
  it_band: [[40, 146, "right"], [80, 146, "left"]],
  quad: [[50, 146, "right"], [70, 146, "left"]],
  knee: [[50, 172, "right"], [70, 172, "left"]],
  shin: [[50, 198, "right"], [70, 198, "left"]],
  ankle: [[50, 223, "right"], [70, 223, "left"]],
  foot: [[47, 236, "right"], [73, 236, "left"]],
  toe: [[44, 245, "right"], [76, 245, "left"]],
};
const BACK = {
  upper_back: [[60, 60]],
  lower_back: [[60, 96]],
  glute: [[50, 118, "left"], [70, 118, "right"]],
  hamstring: [[50, 148, "left"], [70, 148, "right"]],
  calf: [[50, 196, "left"], [70, 196, "right"]],
  achilles: [[50, 221, "left"], [70, 221, "right"]],
  heel: [[50, 238, "left"], [70, 238, "right"]],
};

function entryFlags(entryForms) {
  if (entryForms === false || entryForms == null) return { checkin: false, note: false, pain: false };
  if (entryForms === true) return { checkin: true, note: true, pain: true };
  return {
    checkin: entryForms.checkin !== false,
    note: entryForms.note !== false,
    pain: entryForms.pain !== false,
  };
}

/** Draw the diary (history, complaints and the full entry forms) into `root` on the start page.
 * `section` is kept in the address when the period changes, so the page reopens here. */
export async function mountDiary(root, ctx, { embedded = false, hashBase, skipHeading = false, entryForms = true, section = null } = {}) {
  const forms = entryFlags(entryForms);
  const { query, meta, stale } = ctx;
  const today = meta.data.today;
  const value = mergePeriodValue(query, STORAGE, DEFAULT, { today });
  const reply = await get("diary", apiParams(value, today));
  if (stale()) return;
  if (!embedded) document.title = `${t("Diary")} · VibeSynergy`;
  const { data } = reply;
  const todayCheckin = forms.checkin ? data.checkins.find((c) => c.date === today) || {} : {};
  const state = { part: null, side: null };
  const routeBase = hashBase || "#/dashboard";
  const intro = embedded
    ? (skipHeading ? "" : `<h2 class="diary-heading" id="day-diary-full">${t("Diary")}</h2><p class="sub">${t("Your notes, how you feel, and what hurts.")}</p>`)
    : `<div class="page-head"><h1>${t("Diary")}</h1><div class="sub">${t("Your notes, how you feel, and what hurts.")}</div></div>`;
  const entryBlock = forms.checkin || forms.note || forms.pain
    ? `<section class="grid diary-entry">
      ${forms.checkin ? `<form class="card diary-checkin">
        <h3>${t("Check-in")}</h3>
        <label class="diary-date">${t("Date")} <input class="btn" type="date" name="date" value="${esc(today)}" max="${esc(today)}"></label>
        ${Object.entries(CHECKIN_LABELS).map(([k, [label, low, high]]) => scale(k, t(label), t(low), t(high), todayCheckin[k])).join("")}
        <label class="diary-text">${t("Note")} <input class="btn wide" name="note" maxlength="500" value="${esc(todayCheckin.note || "")}" placeholder="${esc(t("optional"))}"></label>
        <div class="settings-save"><button class="btn primary" type="submit">${t("Save check-in")}</button></div>
      </form>` : ""}
      ${forms.note ? `<form class="card diary-note">
        <h3>${t("Note")}</h3>
        <div class="diary-dates">
          <label>${t("Date")} <input class="btn" type="date" name="date" value="${esc(today)}" required></label>
          <label>${t("Until")} <input class="btn" type="date" name="end" placeholder="${esc(t("optional"))}"></label>
        </div>
        <label class="diary-text">${t("What happened")} <textarea class="diary-textarea" name="text" maxlength="5000" required placeholder="${esc(t("e.g. Cold since yesterday; stayed home"))}"></textarea></label>
        <div class="diary-tags" role="group" aria-label="${esc(t("Tags"))}">${data.choices.tags.map((tag) => `<label class="tag-pick"><input type="checkbox" name="tag" value="${esc(tag)}"><span>${esc(tagName(tag))}</span></label>`).join("")}</div>
        <label class="diary-text">${t("Other tags")} <input class="btn wide" name="more_tags" placeholder="${esc(t("comma separated"))}"></label>
        <div class="settings-save"><button class="btn primary" type="submit">${t("Save note")}</button></div>
      </form>` : ""}
    </section>
    ${forms.pain ? `<section class="card diary-pain">
      <h3>${t("Pain or injury")}</h3>
      <div class="diary-pain-grid">
        <div class="body-maps" role="group" aria-label="${esc(t("Body map: choose where it hurts"))}">
          <figure><figcaption>${t("Front")}</figcaption>${bodyMap(FRONT, "front", data.injuries)}</figure>
          <figure><figcaption>${t("Back")}</figcaption>${bodyMap(BACK, "back", data.injuries)}</figure>
        </div>
        <form class="diary-pain-form settings-grid">
          <label>${t("Where")} ${select("body_part", [["", t("Choose on the map…")], ...data.choices.body_parts.map((p) => [p, partName(p)])], "")}</label>
          <label>${t("Side")} ${select("side", data.choices.sides.map((s) => [s, s === "none" ? t("Centre / n.a.") : cap(t(s))]), "none")}</label>
          <label>${t("Pain today")} <span class="pain-value">3/10</span><input type="range" name="pain" min="0" max="10" value="3"></label>
          <label>${t("When")} ${select("timing", data.choices.timings.map((w) => [w, t(TIMING_LABELS[w])]), "after")}</label>
          <label>${t("Kind")} ${select("kind", data.choices.kinds.map((k) => [k, cap(t(k))]), "niggle")}</label>
          <label>${t("Date")} <input class="btn" type="date" name="date" value="${esc(today)}" max="${esc(today)}"></label>
          <label class="check"><input type="checkbox" name="affects_movement"> ${t("It changes how I move")}</label>
          <label class="diary-text">${t("Note")} <input class="btn wide" name="note" maxlength="500" placeholder="${esc(t("e.g. tight after the long run"))}"></label>
          <div class="settings-save"><button class="btn primary" type="submit">${t("Log pain")}</button></div>
        </form>
      </div>
      <p class="sub">${esc(t(data.note))}</p>
    </section>
` : ""}
`
    : "";

  root.innerHTML = `
    ${intro}
    <form class="filters">${periodPickerHtml({ id: "period", value, extra: EXTRA, today })}</form>
    <div class="flash" role="status"></div>
    ${entryBlock}
    <section class="diary-injuries">${data.injuries.length ? data.injuries.map((i) => injuryCard(i)).join("") : `<div class="card empty">${value.period === "all" ? t("No injuries logged.") : t("No injuries logged in this period.")}</div>`}</section>
    <section class="card">
      <h3>${t("Day by day")}</h3>
      ${historyHtml(data)}
    </section>`;

  const clearFlash = () => {
    const flash = root.querySelector(".flash");
    if (flash) flash.innerHTML = "";
  };
  const again = async (message) => {
    await mountDiary(root, ctx, { embedded, hashBase: routeBase, skipHeading, entryForms, section });
    const flash = root.querySelector(".flash");
    if (flash) flash.innerHTML = message ? `<div class="notice success">${esc(message)}</div>` : "";
  };
  const failed = (error) => {
    root.querySelector(".flash").innerHTML = `<div class="notice error">${esc(error.message)}</div>`;
    root.querySelector(".flash").scrollIntoView({ block: "nearest" });
  };

  bindPeriodPicker(root, {
    id: "period",
    value,
    extra: EXTRA,
    today,
    onChange: (next) => {
      saveStored(STORAGE, next);
      const params = writePeriodQuery(new URLSearchParams(query), next, { defaultValue: DEFAULT });
      if (section) params.set("section", section);
      const qs = params.toString();
      const base = routeBase.split("?")[0];
      location.hash = `${base}${qs ? `?${qs}` : ""}`;
    },
  });

  root.addEventListener("click", (event) => {
    const button = event.target.closest("button");
    if (!button || button.type !== "submit") return;
    clearFlash();
  });

  if (!forms.checkin && !forms.note && !forms.pain) return;

  // Check-in
  const checkinForm = root.querySelector(".diary-checkin");
  checkinForm?.addEventListener("submit", async (event) => {
    event.preventDefault();
    clearFlash();
    const f = new FormData(checkinForm);
    const body = { date: f.get("date") };
    const note = (f.get("note") || "").trim();
    if (note) body.note = note;
    for (const k of Object.keys(CHECKIN_LABELS)) if (f.get(k)) body[k] = Number(f.get(k));
    if (Object.keys(body).length === 1) {
      const flash = root.querySelector(".flash");
      if (flash) {
        flash.innerHTML = `<div class="notice error" role="alert">${esc(t("Answer at least one question"))}</div>`;
      }
      return;
    }
    try {
      await post("diary/checkin", body);
      await again(t("Check-in saved."));
    } catch (error) {
      failed(error);
    }
  });
  if (checkinForm) {
    checkinForm.querySelector("[name=date]").addEventListener("change", (event) => {
      const c = data.checkins.find((x) => x.date === event.target.value) || {};
      for (const k of Object.keys(CHECKIN_LABELS)) {
        for (const input of checkinForm.querySelectorAll(`[name=${k}]`)) input.checked = Number(input.value) === c[k];
      }
      checkinForm.querySelector("[name=note]").value = c.note || "";
    });
  }

  // Note
  const noteForm = root.querySelector(".diary-note");
  noteForm?.addEventListener("submit", async (event) => {
    event.preventDefault();
    clearFlash();
    const f = new FormData(noteForm);
    const tags = [...f.getAll("tag"), ...String(f.get("more_tags") || "").split(",")].map((tag) => tag.trim()).filter(Boolean);
    try {
      await post("diary/note", { text: f.get("text"), date: f.get("date"), end: f.get("end") || null, tags });
      await again(t("Note saved."));
    } catch (error) {
      failed(error);
    }
  });

  // Pain: the map picks the place, the form sends it.
  const painForm = root.querySelector(".diary-pain-form");
  if (painForm) {
  const partSelect = painForm.querySelector("[name=body_part]");
  const sideSelect = painForm.querySelector("[name=side]");
  const showSelection = () => {
    for (const dot of root.querySelectorAll(".body-dot")) {
      dot.classList.toggle("selected", dot.dataset.part === state.part && (dot.dataset.side || "none") === state.side);
    }
  };
  const pick = (part, side) => {
    state.part = part;
    state.side = side || "none";
    partSelect.value = part;
    sideSelect.value = state.side;
    const open = data.injuries.find((i) => i.status !== "resolved" && i.body_part === part && i.side === state.side);
    if (open) painForm.querySelector("[name=kind]").value = open.kind;
    showSelection();
  };
  for (const dot of root.querySelectorAll(".body-dot")) {
    dot.addEventListener("click", () => pick(dot.dataset.part, dot.dataset.side));
    dot.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        pick(dot.dataset.part, dot.dataset.side);
      }
    });
  }
  partSelect.addEventListener("change", () => {
    state.part = partSelect.value;
    state.side = sideSelect.value;
    showSelection();
  });
  sideSelect.addEventListener("change", () => {
    state.side = sideSelect.value;
    showSelection();
  });
  const range = painForm.querySelector("[name=pain]");
  range.addEventListener("input", () => {
    painForm.querySelector(".pain-value").textContent = `${range.value}/10`;
  });
  painForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    clearFlash();
    const f = new FormData(painForm);
    if (!f.get("body_part")) return failed(new Error(t("Choose where it hurts on the map or in the list.")));
    try {
      const reply = await post("diary/injury", {
        body_part: f.get("body_part"),
        side: f.get("side"),
        pain: Number(f.get("pain")),
        timing: f.get("timing"),
        kind: f.get("kind"),
        date: f.get("date"),
        affects_movement: f.get("affects_movement") === "on",
        note: f.get("note") || null,
      });
      const stop = reply.data.flags.find((x) => x.level === "stop");
      await again(stop ? stop.message : t("Logged: {title}, {n}/10.", { title: reply.data.title, n: f.get("pain") }));
    } catch (error) {
      failed(error);
    }
  });
  }

  // Injury cards: status buttons and notes.
  for (const card of root.querySelectorAll(".injury")) {
    const id = card.dataset.id;
    for (const button of card.querySelectorAll("[data-status]")) {
      button.addEventListener("click", async () => {
        try {
          await post("diary/injury", { injury_id: id, status: button.dataset.status });
          await again(t("Marked {status}.", { status: t(button.dataset.status) }));
        } catch (error) {
          failed(error);
        }
      });
    }
    const form = card.querySelector("form.injury-notes");
    form?.addEventListener("submit", async (event) => {
      event.preventDefault();
      clearFlash();
      const text = new FormData(form).get("notes");
      try {
        await post("diary/injury", { injury_id: id, notes: text });
        await again(t("Added to the injury's notes."));
      } catch (error) {
        failed(error);
      }
    });
    const chartEl = card.querySelector(".chart");
    const injury = data.injuries.find((i) => i.id === id);
    if (chartEl && injury) painChart(chartEl, injury, data.load, reply.meta.period);
  }

  // History: delete a note.
  for (const button of root.querySelectorAll("[data-delete-note]")) {
    button.addEventListener("click", async () => {
      if (button.dataset.confirm !== "1") {
        button.dataset.confirm = "1";
        button.textContent = t("Really delete?");
        return;
      }
      try {
        await post("diary/note", { id: button.dataset.deleteNote, delete: true });
        await again(t("Note deleted."));
      } catch (error) {
        failed(error);
      }
    });
  }
}

// Pieces ------------------------------------------------------------------------------

function select(name, options, current) {
  return `<select class="btn" name="${name}">${options
    .map(([v, label]) => `<option value="${esc(v)}"${v === current ? " selected" : ""}>${esc(label)}</option>`)
    .join("")}</select>`;
}

function scale(name, label, low, high, current) {
  const options = [1, 2, 3, 4, 5]
    .map((n) => `<label><input type="radio" name="${name}" value="${n}"${n === current ? " checked" : ""}><span>${n}</span></label>`)
    .join("");
  return `<fieldset class="scale"><legend>${esc(label)}</legend><div class="scale-row"><span class="scale-end">${esc(low)}</span><div class="scale-options">${options}</div><span class="scale-end">${esc(high)}</span></div></fieldset>`;
}

function cap(text) {
  return text.charAt(0).toUpperCase() + text.slice(1);
}

function partName(part) {
  return part === "it_band" ? t("IT band") : cap(t(part.replace(/_/g, " ")));
}

function painColor(pain) {
  if (pain === null || pain === undefined) return token("muted");
  if (pain <= 3) return token("msg-success");
  if (pain <= 6) return token("msg-warning-border");
  return token("msg-danger");
}

function bodyMap(parts, view, injuries) {
  const open = injuries.filter((i) => i.status !== "resolved");
  const dots = Object.entries(parts)
    .flatMap(([part, spots]) =>
      spots.map(([x, y, side]) => {
        const s = side || "none";
        const hurt = open.find((i) => i.body_part === part && (i.side === s || i.side === "both" || (!side && i.side !== "none")));
        const label = `${side ? `${cap(t(side))} ` : ""}${partName(part).toLowerCase()}${hurt ? `, ${hurt.latest_pain ?? "?"}/10` : ""}`;
        const fill = hurt ? painColor(hurt.latest_pain) : "var(--surface)";
        return `<g class="body-dot${hurt ? " hurt" : ""}" data-part="${part}"${side ? ` data-side="${side}"` : ""} role="button" tabindex="0" aria-label="${esc(label)}"><title>${esc(label)}</title><circle cx="${x}" cy="${y}" r="${hurt ? 5.5 : 4}" style="fill:${fill}"/></g>`;
      }),
    )
    .join("");
  const [l, r] = view === "front" ? [t("R"), t("L")] : [t("L"), t("R")];
  return `<svg class="body-map" viewBox="0 0 120 252" width="160" height="336">
    <g class="silhouette">
      <circle cx="60" cy="16" r="12"/>
      <path d="M44 40 H76 Q84 40 84 50 L80 108 Q60 116 40 108 L36 50 Q36 40 44 40 Z"/>
      <path class="limb arm" d="M38 48 L28 84 L20 121"/><path class="limb arm" d="M82 48 L92 84 L100 121"/>
      <path class="limb leg" d="M50 110 L50 172 L50 232"/><path class="limb leg" d="M70 110 L70 172 L70 232"/>
    </g>
    <text x="6" y="248" class="side-mark">${l}</text><text x="108" y="248" class="side-mark">${r}</text>
    ${dots}</svg>`;
}

function injuryCard(i) {
  const flags = i.flags
    .map((f) => `<div class="notice ${FLAG_CLASS[f.level] ?? ""}">${f.level === "stop" ? `<b>${t("Stop.")}</b> ` : ""}${esc(f.message)}</div>`)
    .join("");
  const buttons = i.status === "resolved"
    ? `<button class="btn" type="button" data-status="active">${t("Reopen")}</button>`
    : `${i.status === "active" ? `<button class="btn" type="button" data-status="improving">${t("Improving")}</button>` : `<button class="btn" type="button" data-status="active">${t("Worse again")}</button>`}
       <button class="btn" type="button" data-status="resolved">${t("Resolved")}</button>`;
  const since = t("since {date}", { date: shortDate(i.onset) }) + (i.resolved_on ? ` · ${t("resolved {date}", { date: shortDate(i.resolved_on) })}` : "");
  return `<article class="card injury" data-id="${esc(i.id)}">
    <div class="page-head"><h3>${esc(i.title)}</h3>
      <div class="injury-meta"><span class="chip ${STATUS_CHIP[i.status]}">${esc(t(i.status))}</span> ${esc(t(i.kind))} · ${esc(since)}${i.latest_pain !== null ? ` · ${esc(t("latest {n}/10, worst {worst}/10", { n: i.latest_pain, worst: i.worst_pain }))}` : ""}</div></div>
    ${flags}
    ${i.pain_log.length ? `<div class="legend"><span><i style="background:var(--s2)"></i>${t("Pain (0–10)")}</span><span><i style="background:var(--s1)"></i><span class="load-name">${t("Training load")}</span></span></div><div class="chart injury-chart" role="img" aria-label="${esc(t("Daily pain next to training"))}"></div>` : `<div class="empty">${t("No pain logged yet.")}</div>`}
    ${i.notes ? `<pre class="injury-notes-text">${esc(i.notes)}</pre>` : ""}
    <div class="injury-actions">${buttons}
      <form class="injury-notes"><input class="btn" name="notes" maxlength="1000" placeholder="${esc(t("Add a note: physio advice, diagnosis, return-to-run step"))}" required><button class="btn" type="submit">${t("Add")}</button></form>
    </div>
  </article>`;
}

// Within a day, what happened comes first, then how you felt, then what you wrote about it.
const KIND_ORDER = { event: 0, checkin: 1, note: 2 };

function noteBody(item, extra = "") {
  const tags = item.tags.map((tag) => tagChip(tag, tagName(tag))).join(" ");
  return `${tags} ${esc(item.text)}<div class="sub">${esc(sourceName(item.source))}${extra} · <button class="linkish" type="button" data-delete-note="${esc(item.id)}">${t("Delete")}</button></div>`;
}

function historyHtml(data) {
  const events = data.events || [];
  const byGroup = Object.fromEntries(events.map((e) => [e.group_id, e]));
  // A Garmin and a Strava note about the same merged activity (e.g. its feel and its
  // description) belong on that activity's line, not as separate look-alike entries.
  const attached = new Map();
  const standaloneNotes = [];
  for (const n of data.notes) {
    if (n.activity_id && byGroup[n.activity_id]) {
      (attached.get(n.activity_id) || attached.set(n.activity_id, []).get(n.activity_id)).push(n);
    } else {
      standaloneNotes.push(n);
    }
  }

  const entries = [
    ...events.map((e) => ({ date: e.local_date, kind: "event", item: e, notes: attached.get(e.group_id) || [] })),
    ...standaloneNotes.map((n) => ({ date: n.start_date, kind: "note", item: n })),
    ...data.checkins.map((c) => ({ date: c.date, kind: "checkin", item: c })),
  ].sort((a, b) => (a.date < b.date ? 1 : a.date > b.date ? -1 : KIND_ORDER[a.kind] - KIND_ORDER[b.kind]));
  if (!entries.length) return `<div class="empty">${t("No notes, check-ins or workouts in this period yet.")}</div>`;
  return `<ul class="diary-history">${entries
    .map(({ date, kind, item, notes }) => {
      const day = `<span class="when">${esc(shortDate(date))}</span>`;
      if (kind === "event") {
        const bits = [item.distance_m ? km(item.distance_m) : null, item.moving_s ? hoursMinutes(item.moving_s) : null].filter(Boolean);
        const own = (notes || []).map((n) => `<li>${noteBody(n)}</li>`).join("");
        return `<li>${day}<div><span class="chip">${t("workout")}</span> <a href="#/activity/${encodeURIComponent(item.group_id)}">${esc(item.name || sportName(item.sport))}</a>${bits.length ? ` · ${esc(bits.join(" · "))}` : ""}${own ? `<ul class="diary-event-notes">${own}</ul>` : ""}</div></li>`;
      }
      if (kind === "checkin") {
        const scores = Object.entries(CHECKIN_LABELS)
          .filter(([k]) => item[k] !== null && item[k] !== undefined)
          .map(([k, [label, lowKey, highKey]]) => {
            const low = t(lowKey);
            const high = t(highKey);
            return `${esc(t(label))} <b title="1 = ${esc(low)}, 5 = ${esc(high)}">${item[k]}</b>`;
          })
          .join(" · ");
        return `<li>${day}<div><span class="chip recovery">${t("check-in")}</span> ${scores}${item.note ? `<div>${esc(item.note)}</div>` : ""}</div></li>`;
      }
      const range = item.end_date !== item.start_date ? ` – ${esc(shortDate(item.end_date))}` : "";
      // A standalone note can still be tied to an activity outside this page's events (e.g. an
      // edge case at the edge of the period): keep its own link back in that case.
      // It says which workout (date, sport, name) and links to it.
      const activity = item.activity_id && !byGroup[item.activity_id] ? noteActivityHtml(item) : "";
      return `<li>${day}${range ? `<span class="when">${range}</span>` : ""}<div>${noteBody(item)}${activity}</div></li>`;
    })
    .join("")}</ul>`;
}

// Pain bars next to the daily training load, over the page's period (or since onset).
function painChart(el, injury, load, period) {
  const start = [injury.onset, period.start].sort()[1];
  const end = injury.resolved_on && injury.resolved_on < period.end ? injury.resolved_on : period.end;
  const days = [];
  for (let d = new Date(`${start}T00:00:00Z`); d.toISOString().slice(0, 10) <= end; d.setUTCDate(d.getUTCDate() + 1)) {
    days.push(d.toISOString().slice(0, 10));
  }
  const pain = Object.fromEntries(injury.pain_log.map((e) => [e.date, e]));
  const iLoad = load.columns.indexOf("activity.load");
  const iTime = load.columns.indexOf("activity.duration");
  const byDay = Object.fromEntries(load.rows.map((r) => [r[0], r]));
  const hasLoad = load.rows.some((r) => r[iLoad] !== null);
  const loadName = hasLoad ? t("Training load") : t("Training time (min)");
  const loadValue = (d) => {
    const row = byDay[d];
    if (!row) return 0;
    return hasLoad ? row[iLoad] ?? 0 : Math.round((row[iTime] ?? 0) / 60);
  };
  const muted = token("muted");
  const orange = series(2);
  const loads = days.map(loadValue);
  const anyLoad = loads.some((v) => v > 0);
  const legendName = el.parentElement.querySelector(".load-name");
  if (legendName) legendName.textContent = anyLoad ? loadName : t("No training in this period");
  mount(el, {
    grid: { left: 36, right: 44, top: 12, bottom: 28 },
    tooltip: {
      ...tooltipBase(),
      trigger: "axis",
      formatter: (items) => {
        const d = items[0].axisValue;
        const e = pain[d];
        const lines = [`<b>${esc(shortDate(d))}</b>`];
        if (e) lines.push(`${esc(t("Pain {n}/10", { n: e.pain }))} · ${esc(TIMING_LABELS[e.timing] ? t(TIMING_LABELS[e.timing]) : e.timing)}${e.affects_movement ? ` · ${t("changes movement")}` : ""}${e.note ? `<br>${esc(e.note)}` : ""}`);
        lines.push(`${esc(loadName)} ${esc(number(loadValue(d)))}`);
        return lines.join("<br>");
      },
    },
    xAxis: { type: "category", data: days, axisLabel: { color: muted, formatter: (d) => shortDate(d), hideOverlap: true }, axisLine: { lineStyle: { color: token("axis") } } },
    yAxis: [
      { type: "value", min: 0, max: 10, interval: 2, axisLabel: { color: muted }, splitLine: { lineStyle: { color: token("grid") } } },
      { type: "value", min: 0, show: anyLoad, axisLabel: { color: muted }, splitLine: { show: false } },
    ],
    series: [
      {
        name: loadName,
        type: "line",
        yAxisIndex: 1,
        data: loads,
        showSymbol: false,
        lineStyle: { width: 1.5, color: series(1) },
        areaStyle: { color: alpha(series(1), 0.08) },
        z: 1,
      },
      {
        name: t("Pain"),
        type: "bar",
        data: days.map((d) => (pain[d] ? { value: pain[d].pain, itemStyle: { color: pain[d].affects_movement ? token("msg-danger") : orange } } : null)),
        barMaxWidth: 12,
        z: 2,
      },
    ],
  });
}
