// A workout or activity named anywhere in the portal (#181): one look and behaviour for
// both forms. Done, its title links to the activity (#/activity/<id>); still planned with
// a workout text from VibeSynergy's plan, it opens (details/summary, keyboard reachable)
// to show the steps, as the week progress does; with neither, it stays plain text.
// The server adds the activity id and the workout text; this only renders them.
import { esc } from "./format.js";
import { workoutHtml } from "./workout_text.js";

/**
 * @param {{title?: string, activity_id?: string|number|null, workout?: string|null}} ref
 * @param {{note?: string, label?: (html: string) => string, linkActivities?: boolean}} [options]
 *   `note` is extra HTML after the title (already escaped); `label` wraps the escaped title
 *   (a sport icon, say); `linkActivities` false keeps a done one plain (a shared view).
 */
export function workoutRefHtml(ref, { note = "", label = (html) => html, linkActivities = true } = {}) {
  const title = label(esc(ref?.title || ""));
  const id = ref?.activity_id;
  if (id !== undefined && id !== null && id !== "" && linkActivities) {
    return `<a href="#/activity/${esc(encodeURIComponent(String(id)))}" data-workout-activity>${title}</a>${note}`;
  }
  if (ref?.workout) {
    return `<details class="collapsible workout-ref" data-workout-steps><summary><span>${title}${note}</span></summary>${workoutHtml(ref.workout)}</details>`;
  }
  return `${title}${note}`;
}
