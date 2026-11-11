import assert from "node:assert/strict";
import test from "node:test";

import { bindAutosave } from "../src/vibesynergy/portal/static/js/settings_autosave.js";

// Minimal stand-ins for a settings section and one of its fields; no DOM needed.
function field() {
  return { matches: (selector) => selector.includes("input") && !selector.includes("file") };
}

function status() {
  return {
    textContent: "",
    isConnected: true,
    replaceChildren() {
      this.textContent = "";
    },
    classList: { toggle() {}, add() {} },
  };
}

function fire(root, type, target) {
  root.dispatchEvent({ type, target });
}

// A plain listener table: a real EventTarget would make event.target read-only.
function section() {
  const listeners = {};
  return {
    addEventListener: (type, fn) => {
      (listeners[type] ||= []).push(fn);
    },
    removeEventListener: (type, fn) => {
      listeners[type] = (listeners[type] || []).filter((f) => f !== fn);
    },
    dispatchEvent: (event) => (listeners[event.type] || []).forEach((fn) => fn(event)),
  };
}

test("blur without an edit does not save", async () => {
  const root = section();
  let saves = 0;
  bindAutosave(root, { save: async () => saves++, statusEl: status(), debounceMs: 1 });
  fire(root, "focusout", field());
  await new Promise((resolve) => setTimeout(resolve, 10));
  assert.equal(saves, 0);
});

test("blur after typing saves once", async () => {
  const root = section();
  let saves = 0;
  const statusEl = status();
  bindAutosave(root, { save: async () => saves++, statusEl, debounceMs: 1 });
  const input = field();
  fire(root, "input", input);
  fire(root, "focusout", input);
  await new Promise((resolve) => setTimeout(resolve, 10));
  assert.equal(saves, 1);
  assert.equal(statusEl.textContent, "Saved.");
  fire(root, "focusout", input);
  await new Promise((resolve) => setTimeout(resolve, 10));
  assert.equal(saves, 1);
});

test("a change saves after the debounce and an explicit flush always saves", async () => {
  const root = section();
  let saves = 0;
  const save = bindAutosave(root, { save: async () => saves++, statusEl: status(), debounceMs: 1 });
  fire(root, "change", field());
  await new Promise((resolve) => setTimeout(resolve, 10));
  assert.equal(saves, 1);
  await save.flush();
  assert.equal(saves, 2);
});

test("typing inside a block marked data-autosave-skip never saves on blur", async () => {
  const root = section();
  let saves = 0;
  bindAutosave(root, { save: async () => saves++, statusEl: status(), debounceMs: 1 });
  // A field of the AI coach links card: it has its own save button.
  const input = { ...field(), closest: (selector) => (selector === "[data-autosave-skip]" ? {} : null) };
  fire(root, "input", input);
  fire(root, "change", input);
  fire(root, "focusout", input);
  await new Promise((resolve) => setTimeout(resolve, 10));
  assert.equal(saves, 0);
});
