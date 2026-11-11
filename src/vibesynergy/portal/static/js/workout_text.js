// A plan workout's text (intervals.icu workout syntax) as structure you can scan:
// the intro as text, section heads (warm-up, main set, 8x), one line per step with the zone
// mark (Z1…Z7 swatch + label, never colour alone), and a strength block ([garmin]) as
// exercises with sets, reps, weight and rest. Everything is escaped; unknown lines stay
// text, so nothing the coach wrote disappears.
import { esc } from "./format.js";
import { t } from "./i18n.js";
import { zoneBadge } from "./zone_presentation.js";

const REPEAT = /^(\d+)\s*x$/i;
const STEP = /^-\s+/;

// One step's text with its zone marks and rest wording.
function stepHtml(text) {
  return esc(text)
    .replace(/\bintensity=rest\b/g, esc(t("rest")))
    .replace(/\bZ([1-7])\b/g, (_, n) => zoneBadge(Number(n)));
}

function duration(value) {
  const m = /^(\d+)(s|m|h)$/.exec(value || "");
  if (!m) return value || "";
  const n = Number(m[1]);
  if (m[2] === "h") return t("{n} h", { n });
  if (m[2] === "m") return t("{n} min", { n });
  return n >= 60 && n % 60 === 0 ? t("{n} min", { n: n / 60 }) : t("{n} s", { n });
}

// `SQUAT/BARBELL_BACK_SQUAT sets=3 reps=10 rest=120s weight=60kg "Back squat"` → parts.
export function parseExercise(line) {
  const label = /"([^"]*)"\s*$/.exec(line)?.[1] || "";
  const head = line.replace(/"[^"]*"\s*$/, "").trim().split(/\s+/);
  const options = Object.fromEntries(
    head.slice(1).filter((p) => p.includes("=")).map((p) => p.split("=", 2)),
  );
  const name = head[0] || "";
  const fallback = (name.split("/").pop() || name).replaceAll("_", " ").toLowerCase();
  return { name, label: label || fallback, options };
}

function exerciseHtml(line) {
  const { label, options } = parseExercise(line);
  const facts = [];
  if (options.sets && options.reps) facts.push(t("{sets} × {reps}", { sets: options.sets, reps: options.reps }));
  else if (options.reps) facts.push(t("{reps} reps", { reps: options.reps }));
  if (options.time) facts.push((options.sets && options.sets !== "1" ? `${options.sets} × ` : "") + duration(options.time));
  if (options.side === "both") facts.push(t("both sides"));
  if (options.weight) {
    const kg = /^(\d+(?:[.,]\d+)?)(kg|lb)$/.exec(options.weight);
    facts.push(options.weight === "bw" ? t("bodyweight") : kg ? `${kg[1]} ${kg[2]}` : options.weight);
  }
  if (options.rest) facts.push(t("rest {time}", { time: duration(options.rest) }));
  const sub = facts.length ? ` <span class="sub">${esc(facts.join(" · "))}</span>` : "";
  return `<li>${esc(label)}${sub}</li>`;
}

// Split into blocks: text, head, steps, exercises (the [garmin] block, up to a blank line).
export function parseWorkout(description) {
  const lines = String(description || "").replaceAll("\r", "").split("\n");
  const blocks = [];
  let i = 0;
  const next = (n) => (lines[n] || "").trim();
  const push = (type, value) => {
    const last = blocks[blocks.length - 1];
    if (last && last.type === type && type !== "head") last.items.push(value);
    else blocks.push({ type, items: [value] });
  };
  while (i < lines.length) {
    const line = lines[i].trim();
    i += 1;
    if (!line) {
      blocks.push({ type: "gap", items: [] });
      continue;
    }
    if (line.toLowerCase() === "[garmin]") {
      while (i < lines.length && lines[i].trim()) {
        push("exercises", lines[i].trim());
        i += 1;
      }
      continue;
    }
    if (STEP.test(line)) push("steps", line.replace(STEP, ""));
    else if (REPEAT.test(line) || STEP.test(next(i)) || REPEAT.test(next(i))) push("head", line);
    else push("text", line);
  }
  return blocks.filter((b, n) => b.type !== "gap" || (n > 0 && blocks[n - 1].type !== "gap"));
}

export function workoutHtml(description) {
  const blocks = parseWorkout(description);
  if (!blocks.some((b) => b.type === "steps" || b.type === "exercises")) {
    // Free text only: keep it as the coach wrote it.
    return `<div class="plan-note">${esc(String(description || "").trim()).replaceAll("\n", "<br>")}</div>`;
  }
  const html = blocks.map((block) => {
    if (block.type === "text") return `<p>${block.items.map(esc).join("<br>")}</p>`;
    if (block.type === "head") {
      const head = block.items[0];
      const repeat = REPEAT.exec(head);
      return `<p class="workout-head"><b>${esc(repeat ? t("{n}× repeat", { n: repeat[1] }) : head)}</b></p>`;
    }
    if (block.type === "steps") return `<ul class="workout-steps">${block.items.map((s) => `<li>${stepHtml(s)}</li>`).join("")}</ul>`;
    if (block.type === "exercises") return `<ol class="workout-exercises">${block.items.map(exerciseHtml).join("")}</ol>`;
    return "";
  });
  return `<div class="plan-note workout-text">${html.join("")}</div>`;
}
