// Coach → Settings → Coach via API: run the coach inside VibeSynergy on your own Claude API
// credits. Off by default. The API key is write-only: the page sends it once and only ever
// sees api_key_set. The switch, the monthly limit and the schedule save as you change them;
// "Test now" makes one real report right away (it costs like any run).
import { get, post } from "./api.js";
import { esc, number, userDateTime } from "./format.js";
import { N, t } from "./i18n.js";
import { bindAutosave } from "./settings_autosave.js";

const JOBS = {
  morning: N("Morning advice"),
  evening: N("Evening review"),
  weekly: N("Week review"),
  monthly: N("Month report"),
  block: N("Block review"),
  race: N("Race brief"),
  test: N("Test evaluation"),
  thresholds: N("Threshold check"),
  kb: N("Knowledge base care"),
  injury: N("Injury check"),
  checkin: N("Check-in reaction"),
  insight: N("Line under a public activity"),
};
const STATUS = {
  done: N("Done"),
  running: N("Running"),
  failed: N("Did not finish"),
  budget: N("Stopped: monthly limit reached"),
  skipped: N("Skipped"),
};
const REPORTS = ["morning", "evening", "weekly", "monthly", "block", "race", "thresholds", "kb"]; // on a clock
const WITH_REPORT = [...REPORTS, "test", "injury", "checkin"]; // write a mailed report: rules, Test now, Send again
const DAYS = [
  [N("Mon"), N("Monday")],
  [N("Tue"), N("Tuesday")],
  [N("Wed"), N("Wednesday")],
  [N("Thu"), N("Thursday")],
  [N("Fri"), N("Friday")],
  [N("Sat"), N("Saturday")],
  [N("Sun"), N("Sunday")],
];
const PROVIDER_NAMES = { anthropic: "Anthropic (Claude)", openai: "OpenAI", xai: "xAI (Grok)", local: N("Local server") };
const TASK_NAMES = { morning: N("Morning advice"), evening: N("Evening review"), weekly: N("Week review"), monthly: N("Month report"), block: N("Block review"), race: N("Race brief"), test: N("Test evaluation"), thresholds: N("Threshold check"), kb: N("Knowledge base care"), injury: N("Injury check"), checkin: N("Check-in reaction"), insight: N("Line under a public activity"), catch_up: N("Catch up a missed report"), reply: N("Answer to your reply") };
// When an event task starts, in one line.
const EVENT_TEXT = {
  test: N("Starts when a planned test (run test, FTP, CSS) is done and synced. New thresholds come as a proposal you approve."),
  injury: N("Starts when you log pain of 4 out of 10 or more for today, or pain that affects how you move. At most once a day."),
  checkin: N("Starts when your check-in after the morning advice shows a hard day: two or more low scores, before 20:00. At most once a day."),
};
// Tasks that run as code, without a model: a switch and what they do.
const CODE_TASK_TEXT = {
  catch_up: N("If a report did not run at all today (the server was off, say), it is made once, late. A report that failed is not tried again; you already got a message about it."),
};
// What a task reads before it starts. The base (athlete, communication rule,
// injuries, coach memory) goes with every task and is said once above the cards.
const BASE_CONTEXT = ["athlete", "communication_rule", "safety", "coach_memory"];
const CONTEXT_NAMES = {
  today: N("today"),
  recovery: N("recovery"),
  plan_horizon: N("plan for the next 9 days"),
  plan_next_week: N("plan for next week"),
  week: N("this week"),
  weather: N("weather"),
  replies: N("your replies"),
  recent_reports: N("recent reports"),
  fitness: N("fitness"),
  activity: N("the activity"),
  plan_match: N("what was planned that day"),
  month: N("last month"),
  races: N("races"),
  block: N("the block that ends"),
  threshold_signals: N("measured values that differ from your thresholds"),
  kb_due: N("the articles longest without a check"),
};
const MODEL_HINTS = ["claude-opus-5-5", "claude-sonnet-5", "claude-haiku-4-5-20251001", "gpt-5", "gpt-5-mini", "grok-4", "llama3.1", "qwen2.5"];

function providerHtml(p, data) {
  const set = (data.keys || {})[p];
  const id = `coach-runner-key-${p}`;
  const url = p === "local"
    ? `<label for="coach-runner-local-url">${esc(t("Address of the local server"))}</label>
       <input class="btn" id="coach-runner-local-url" name="local_url" type="url" placeholder="http://localhost:11434/v1" value="${esc(data.local_url || "")}">`
    : "";
  // Folded once set up; the first time, Anthropic is open so the first key is one step away.
  const open = !data.api_key_set && p === "anthropic";
  const state = p === "local" && data.local_url ? data.local_url : set ? t("Key saved") : t("Not set");
  return `<details class="settings-field coach-task" data-autosave-skip${open ? " open" : ""}>
    <summary><strong>${esc(t(PROVIDER_NAMES[p] || p))}</strong> <span class="sub">${esc(state)}</span></summary>
    ${url}
    <label for="${id}">${esc(p === "local" ? t("API key (if the server asks for one)") : t("API key"))}</label>
    <input class="btn" id="${id}" type="password" autocomplete="off" spellcheck="false" placeholder="${esc(set ? t("Saved. Type a new one to replace it.") : t("Not set"))}">
    <div class="settings-save"><button class="btn" type="button" data-key-save="${esc(p)}">${esc(t("Save key"))}</button>${set ? `<button class="btn" type="button" data-key-clear="${esc(p)}">${esc(t("Remove key"))}</button>` : ""}</div>
  </details>`;
}

function taskCardHtml(entry, data) {
  const task = entry.id;
  const id = `coach-task-${task}`;
  const name = esc(t(TASK_NAMES[task] || task));
  const loads = entry.context.filter((c) => !BASE_CONTEXT.includes(c)).map((c) => t(CONTEXT_NAMES[c] || c));
  const loadsLine = loads.length ? `<p class="sub">${esc(t("Reads first: {what}", { what: loads.join(", ") }))}</p>` : "";
  if (entry.status !== "live") {
    return `<section class="settings-field coach-task" aria-labelledby="${id}-title"><h4 id="${id}-title">${name}</h4>
      <p class="sub">${esc(t("Not built yet."))}</p>${loadsLine}</section>`;
  }
  const spec = data.tasks[task] || {};
  if (entry.runs === "code") {
    const on = spec.on !== false;
    return `<details class="settings-field coach-task">
    <summary><strong>${name}</strong> <span class="sub">${esc(on ? t("On") : t("Off"))}</span></summary>
    <label class="check"><input type="checkbox" role="switch" name="task-on-${esc(task)}"${on ? " checked" : ""}> ${esc(t("On"))}</label>
    <p class="sub">${esc(t(CODE_TASK_TEXT[task] || ""))}</p>
  </details>`;
  }
  const sched = (data.schedule || {})[task];
  const on = sched ? sched.on : spec.on !== false;
  const switchName = sched ? `${task}-on` : `task-on-${task}`;
  const run = (data.last_runs || {})[task];
  const rules = WITH_REPORT.includes(task)
    ? `<details class="settings-more"><summary>${esc(t("Always added for {report}", { report: t(JOBS[task]).toLowerCase() }))}</summary><pre class="prompt-rules">${esc((data.rules || {})[task] || "")}</pre></details>`
    : "";
  const line = [on ? t("On") : t("Off"), sched ? sched.time : t("after an event"), spec.model].filter(Boolean).join(" · ");
  return `<details class="settings-field coach-task">
    <summary><strong>${name}</strong> <span class="sub">${esc(line)}</span></summary>
    <label class="check"><input type="checkbox" role="switch" name="${switchName}"${on ? " checked" : ""}> ${esc(t("On"))}</label>
    <label for="${id}-provider">${esc(t("Provider"))}</label>
    <select class="btn" id="${id}-provider" name="task-provider-${esc(task)}">${(data.providers || []).map((p) => `<option value="${esc(p)}"${spec.provider === p ? " selected" : ""}>${esc(t(PROVIDER_NAMES[p] || p))}</option>`).join("")}</select>
    <label for="${id}-model">${esc(t("Model"))}</label>
    <input class="btn" id="${id}-model" name="task-model-${esc(task)}" list="coach-runner-models" value="${esc(spec.model || "")}" spellcheck="false">
    ${sched ? scheduleRowHtml(task, sched, data.enabled) : ""}
    ${!sched && EVENT_TEXT[task] ? `<p class="sub">${esc(t(EVENT_TEXT[task]))}</p>` : ""}
    ${!sched && WITH_REPORT.includes(task) ? `<div class="settings-save" data-autosave-skip><button class="btn" type="button" data-runner-test="${esc(task)}"${data.enabled ? "" : " disabled"}>${esc(t("Test now"))}</button></div>` : ""}
    ${loadsLine}
    <p class="sub">${esc(t("Last run"))}: ${lastRunHtml(task, run, data.enabled)}</p>
    ${rules}
  </details>`;
}

const TONE = { done: "good", running: "", failed: "warning", budget: "warning", skipped: "" };

function dollars(value) {
  return `$${number(value, 2)}`;
}

function usageLine(data) {
  const used = data.usage;
  const tokens = used.input_tokens + used.output_tokens + used.cache_write_tokens + used.cache_read_tokens;
  return t("{cost} of {cap} used this month · {runs} runs · {tokens} tokens", {
    cost: dollars(used.cost_usd),
    cap: dollars(data.monthly_cap_usd),
    runs: number(used.runs),
    tokens: number(tokens),
  });
}

// Only when something was off: context the coach had to do without, or a report
// outside its agreed length or type.
function harnessNote(harness) {
  if (!harness) return "";
  const missing = (harness.blocks || []).filter((b) => !b.ok).map((b) => b.name);
  const notes = [];
  if (missing.length) notes.push(t("Started without: {names}", { names: missing.join(", ") }));
  if ((harness.contract || []).length) notes.push(t("Report longer than agreed or of the wrong kind"));
  return notes.length ? ` <span class="sub">${esc(notes.join(" · "))}</span>` : "";
}

function lastRunHtml(job, run, enabled) {
  if (!run) return esc(t("Not run yet"));
  const status = `<span class="chip ${TONE[run.status] || ""}">${esc(t(STATUS[run.status] || run.status))}</span>`;
  const when = esc(userDateTime(run.finished_at || run.started_at));
  const mail = run.mail_error ? ` <span class="sub">${esc(t("Report not mailed: set up mail under Settings → Alerts."))}</span>` : "";
  const report = run.entry_id ? ` · <a href="#/coach/report/${esc(String(run.entry_id))}">${esc(t("Open report"))}</a>` : "";
  const again = WITH_REPORT.includes(job)
    ? ` <span class="settings-save" data-autosave-skip>${run.entry_id ? `<button class="btn" type="button" data-runner-resend="${job}">${esc(t("Send again"))}</button>` : ""}<button class="btn" type="button" data-runner-test="${job}"${enabled ? "" : " disabled"}>${esc(t("Make again"))}</button></span>`
    : "";
  return `${status} · ${when} · ${esc(dollars(run.cost_usd))}${report}${harnessNote(run.harness)}${mail}${again}`;
}

function scheduleRowHtml(job, spec, canTest) {
  const id = `coach-runner-${job}`;
  const days = job === "kb"
    ? `<label for="${id}-monthday">${esc(t("Day of the month"))}</label>
       <input class="btn" id="${id}-monthday" name="${job}-monthday" type="number" min="1" max="28" step="1" inputmode="numeric" value="${esc(String(spec.monthday || 15))}">
       <p class="sub">${esc(t("Checks five articles a month, the oldest first. Changes come as a proposal you approve."))}</p>`
    : job === "race"
    ? `<p class="sub">${esc(t("7 days and 1 day before each race on your calendar."))}</p>`
    : job === "block"
    ? `<p class="sub">${esc(t("On the Sunday a phase of your plan ends."))}</p>`
    : job === "thresholds"
    ? `<p class="sub">${esc(t("On Monday, only when a measured value no longer fits your thresholds; once per finding."))}</p>`
    : job === "monthly"
    ? `<label for="${id}-monthday">${esc(t("Day of the month"))}</label>
       <input class="btn" id="${id}-monthday" name="${job}-monthday" type="number" min="1" max="28" step="1" inputmode="numeric" value="${esc(String(spec.monthday || 1))}">`
    : job === "weekly"
    ? `<label>${esc(t("Day"))} <select class="btn" name="${job}-day">${DAYS.map(([, long], i) => `<option value="${i}"${spec.days.includes(i) ? " selected" : ""}>${esc(t(long))}</option>`).join("")}</select></label>`
    : `<fieldset class="day-picks"><legend>${esc(t("Days"))}</legend>${DAYS.map(([short, long], i) => `<label class="check"><input type="checkbox" name="${job}-days" value="${i}"${spec.days.includes(i) ? " checked" : ""}> <span aria-hidden="true">${esc(t(short))}</span><span class="visually-hidden">${esc(t(long))}</span></label>`).join("")}</fieldset>`;
  return `<fieldset class="coach-task-when">
    <legend>${esc(t("When"))}</legend>
    <label for="${id}-time">${esc(t("Time"))}</label>
    <input class="btn" id="${id}-time" name="${job}-time" type="time" step="60" value="${esc(spec.time)}">
    ${days}
    <div class="settings-save" data-autosave-skip><button class="btn" type="button" data-runner-test="${job}"${canTest ? "" : " disabled"}>${esc(t("Test now"))}</button></div>
  </fieldset>`;
}

function scheduleFrom(el) {
  const out = {};
  for (const job of REPORTS) {
    if (job === "race" || job === "block" || job === "thresholds") {
      out[job] = {
        on: el.querySelector(`[name="${job}-on"]`).checked,
        time: el.querySelector(`[name="${job}-time"]`).value,
      };
      continue;
    }
    if (job === "monthly" || job === "kb") {
      out[job] = {
        on: el.querySelector(`[name="${job}-on"]`).checked,
        time: el.querySelector(`[name="${job}-time"]`).value,
        monthday: Number(el.querySelector(`[name="${job}-monthday"]`).value),
      };
      continue;
    }
    const days = job === "weekly"
      ? [Number(el.querySelector(`[name="${job}-day"]`).value)]
      : [...el.querySelectorAll(`[name="${job}-days"]:checked`)].map((box) => Number(box.value));
    out[job] = {
      on: el.querySelector(`[name="${job}-on"]`).checked,
      time: el.querySelector(`[name="${job}-time"]`).value,
      days,
    };
  }
  return out;
}

function cardHtml(data) {
  const share = Math.min(1, data.usage.share || 0);
  return `
    <h2 id="coach-runner-title">${esc(t("Coach via API"))}</h2>
    <p class="sub">${esc(t("Let your coach run inside VibeSynergy on your own Claude API credits: the morning and evening advice, the Sunday week review and the line under a public activity. Each report comes by mail, and you can reply here."))}</p>
    <label class="check"><input type="checkbox" role="switch" name="enabled" aria-describedby="coach-runner-switch-hint"${data.enabled ? " checked" : ""}${data.api_key_set ? "" : " disabled"}> ${esc(t("Coach via API"))}</label>
    <p class="sub" id="coach-runner-switch-hint">${esc(!data.api_key_set ? t("Add an API key first.") : data.enabled ? t("On: your coach runs here on your API credits.") : t("Off until you switch it on."))}</p>
    ${data.enabled ? `<div class="notice advice" role="note"><p>${esc(t("Turn off the Claude routines for the morning, the evening, the week review and after a workout, and the coach link under Settings → Sync between sources. Otherwise your coach runs twice."))}</p></div>` : ""}
    <div class="settings-grid">
      <div class="settings-field">
        <label for="coach-runner-cap">${esc(t("Monthly limit in dollars"))}</label>
        <input class="btn" id="coach-runner-cap" name="monthly_cap_usd" type="number" min="1" max="1000" step="1" inputmode="numeric" aria-describedby="coach-runner-cap-hint" value="${esc(String(data.monthly_cap_usd))}">
        <p class="sub" id="coach-runner-cap-hint">${esc(t("You get a mail at 80%. At 100% your coach pauses until next month."))}</p>
      </div>
      <div class="settings-field" data-autosave-skip>
        <label for="coach-runner-name">${esc(t("Your name"))}</label>
        <input class="btn" id="coach-runner-name" name="athlete_name" maxlength="40" autocomplete="given-name" aria-describedby="coach-runner-name-hint" value="${esc(data.athlete_name && data.athlete_name !== "de atleet" ? data.athlete_name : "")}">
        <p class="sub" id="coach-runner-name-hint">${esc(t("How your coach addresses you in reports."))}</p>
      </div>
    </div>
    <h3 class="settings-subhead">${esc(t("AI providers"))}</h3>
    <p class="sub">${esc(t("Keys are kept encrypted and never shown again. OpenAI and xAI do not let other apps sign in with your ChatGPT or Grok account, so those take an API key."))}</p>
    <div class="settings-grid">${(data.providers || []).map((p) => providerHtml(p, data)).join("")}</div>
    <h3 class="settings-subhead">${esc(t("Tasks"))}</h3>
    <p class="sub">${esc(t("Each task can be switched on or off and has its own model. Every task knows you, your injuries and what your coach remembers. Times are in {zone}. Test now makes one real report right away and mails it; it costs like any run.", { zone: data.timezone }))}</p>
    <p class="sub"><a href="#/coach/methods">${esc(t("Change what each report does"))}</a></p>
    <div class="settings-grid">${(data.registry || []).map((entry) => taskCardHtml(entry, data)).join("")}</div>
    <datalist id="coach-runner-models">${MODEL_HINTS.map((m) => `<option value="${esc(m)}"></option>`).join("")}</datalist>
    <h3 class="settings-subhead">${esc(t("This month"))}</h3>
    <p>${esc(usageLine(data))}</p>
    <progress max="1" value="${esc(String(share))}" aria-label="${esc(t("Share of the monthly limit used"))}"></progress>
    <p class="sub" role="status" aria-live="polite" data-runner-status></p>`;
}

const cleanups = new WeakMap();

export async function coachRunnerSettings(el, stale) {
  const { data } = await get("coach-runner");
  if (stale() || !el.isConnected) return;
  draw(el, stale, data);
}

function draw(el, stale, data) {
  cleanups.get(el)?.();
  el.innerHTML = cardHtml(data);
  const status = el.querySelector("[data-runner-status]");
  const redraw = (next) => {
    if (!stale() && el.isConnected) draw(el, stale, next);
  };
  const say = (text) => {
    if (status.isConnected) status.textContent = text;
  };
  const autosave = bindAutosave(el, {
    statusEl: status,
    save: async () => {
      const toggle = el.querySelector('input[name="enabled"]');
      const cap = Number(el.querySelector('input[name="monthly_cap_usd"]').value);
      const changes = {
        monthly_cap_usd: cap,
        schedule: scheduleFrom(el),
        tasks: Object.fromEntries([...el.querySelectorAll("[name^=task-provider-]")].map((sel) => {
          const task = sel.name.slice("task-provider-".length);
          const spec = { provider: sel.value, model: el.querySelector(`[name="task-model-${task}"]`).value.trim() };
          const on = el.querySelector(`[name="task-on-${task}"]`);
          if (on) spec.on = on.checked;
          return [task, spec];
        }).concat([...el.querySelectorAll("[name^=task-on-]")]
          .filter((box) => !el.querySelector(`[name="task-provider-${box.name.slice("task-on-".length)}"]`))
          .map((box) => [box.name.slice("task-on-".length), { on: box.checked }]))),
        local_url: el.querySelector('[name="local_url"]')?.value.trim() ?? "",
      };
      if (!toggle.disabled) changes.enabled = toggle.checked;
      try {
        const { data: next } = await post("coach-runner/settings", changes);
        const wasOn = data.enabled;
        data = next;
        if (wasOn !== next.enabled) redraw(next);
      } catch (error) {
        say(t(error.message));
        throw error;
      }
    },
  });
  cleanups.set(el, () => autosave.unbind());
  el.querySelector('[name="athlete_name"]')?.addEventListener("change", async (event) => {
    const value = event.target.value.trim();
    try {
      await post("prefs", { changes: { athlete_name: value || null } });
      say(t("Saved."));
    } catch (error) {
      say(t(error.message));
    }
  });
  for (const button of el.querySelectorAll("[data-runner-resend]")) {
    button.addEventListener("click", async () => {
      button.disabled = true;
      try {
        const { data: next } = await post("coach-runner/resend", { job: button.dataset.runnerResend });
        redraw(next);
        el.querySelector("[data-runner-status]").textContent = t("Sent again. Check your mail.");
      } catch (error) {
        button.disabled = false;
        say(t(error.message));
      }
    });
  }
  for (const button of el.querySelectorAll("[data-runner-test]")) {
    button.addEventListener("click", async () => {
      button.disabled = true;
      try {
        const { data: next } = await post("coach-runner/test", { job: button.dataset.runnerTest });
        redraw(next);
        el.querySelector("[data-runner-status]").textContent = t("Test started. The report is in your mail in a few minutes and under Last run.");
      } catch (error) {
        button.disabled = false;
        say(t(error.message));
      }
    });
  }
  for (const button of el.querySelectorAll("[data-key-save]")) {
    button.addEventListener("click", async () => {
      const provider = button.dataset.keySave;
      const input = el.querySelector(`#coach-runner-key-${provider}`);
      if (!input.value.trim()) {
        say(t("Paste the key first."));
        input.focus();
        return;
      }
      try {
        const { data: next } = await post("coach-runner/key", { value: input.value, provider });
        input.value = "";
        redraw(next);
        el.querySelector("[data-runner-status]").textContent = t("Saved.");
      } catch (error) {
        say(t(error.message));
      }
    });
  }
  for (const button of el.querySelectorAll("[data-key-clear]")) {
    button.addEventListener("click", async () => {
      try {
        const { data: next } = await post("coach-runner/key/clear", { provider: button.dataset.keyClear });
        redraw(next);
        el.querySelector("[data-runner-status]").textContent = t("Key removed.");
      } catch (error) {
        say(t(error.message));
      }
    });
  }
}
