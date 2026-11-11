// Debounced autosave for settings sections: change (debounced) and blur (flush).
// Blur only saves after an edit: tabbing through a section must not write the config
// or show "Saved." for nothing.
import { t } from "./i18n.js";

const DEFAULT_MS = 400;

export function reportSaveError(statusEl, retry) {
  statusEl.classList.add("error-text");
  statusEl.replaceChildren();
  const text = document.createElement("span");
  text.textContent = t("Couldn't save. Check your connection and try again.");
  const button = document.createElement("button");
  button.type = "button";
  button.className = "btn";
  button.textContent = t("Try again");
  button.addEventListener("click", () => retry());
  statusEl.append(text, document.createTextNode(" "), button);
}

// `onError(error)` may explain a failure itself (return true), e.g. a newer version saved
// elsewhere; otherwise the generic "Couldn't save" with a retry button is shown.
export function bindAutosave(root, { save, statusEl, debounceMs = DEFAULT_MS, onError }) {
  if (!root || !statusEl) return { unbind() {}, flush() {} };
  let timer;
  let inFlight = null;
  let pending = false;
  let dirty = false;

  const say = (text, error = false) => {
    statusEl.replaceChildren();
    statusEl.textContent = text;
    statusEl.classList.toggle("error-text", error);
  };

  const showError = () => reportSaveError(statusEl, () => run().catch(() => {}));

  const run = async () => {
    clearTimeout(timer);
    dirty = false;
    if (inFlight) {
      pending = true;
      return inFlight;
    }
    say(t("Saving…"));
    inFlight = (async () => {
      try {
        await save();
        if (statusEl.isConnected) say(t("Saved."));
      } catch (error) {
        if (statusEl.isConnected && !onError?.(error)) showError();
        throw error;
      }
    })().finally(() => {
      inFlight = null;
      if (pending) {
        pending = false;
        run();
      }
    });
    return inFlight;
  };

  const schedule = () => {
    clearTimeout(timer);
    timer = setTimeout(run, debounceMs);
  };

  // Controls with their own save button (or inside a block marked data-autosave-skip, such as
  // the AI coach links card) never trigger the section's autosave: a blur from switching windows
  // would otherwise save and redraw the tab, wiping a half-filled form.
  const skippable = (target) =>
    target.matches('input[type="file"], [data-import] input') ||
    Boolean(target.closest?.("[data-autosave-skip]"));

  const onInput = (event) => {
    if (skippable(event.target)) return;
    if (event.target.matches("input, select, textarea")) dirty = true;
  };
  const onChange = (event) => {
    if (skippable(event.target)) return;
    if (event.target.matches("input, select, textarea")) {
      dirty = true;
      schedule();
    }
  };
  const onBlur = (event) => {
    if (skippable(event.target)) return;
    if (dirty && event.target.matches("input, textarea, select")) run();
  };

  root.addEventListener("input", onInput);
  root.addEventListener("change", onChange);
  root.addEventListener("focusout", onBlur);

  return {
    flush: run,
    unbind() {
      clearTimeout(timer);
      root.removeEventListener("input", onInput);
      root.removeEventListener("change", onChange);
      root.removeEventListener("focusout", onBlur);
    },
  };
}
