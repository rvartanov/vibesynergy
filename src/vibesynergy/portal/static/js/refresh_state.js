// Only explicitly keyed controls/disclosures survive a render. Keys include the object ID
// where needed, so navigating to a different activity cannot inherit its previous state.
export function captureViewState(root) {
  const active = document.activeElement;
  return {
    open: new Set([...root.querySelectorAll("details[data-refresh-key][open]")].map((el) => el.dataset.refreshKey)),
    focus: root.contains(active) ? active.closest("[data-refresh-key]")?.dataset.refreshKey : null,
    active,
  };
}

export function restoreViewState(root, state) {
  if (!root.isConnected) return;
  for (const el of root.querySelectorAll("details[data-refresh-key]")) {
    el.open = state.open.has(el.dataset.refreshKey);
  }
  // Do not steal focus if the user moved to another still-connected control during I/O.
  if (state.focus && (document.activeElement === document.body || document.activeElement === state.active)) {
    const target = [...root.querySelectorAll("[data-refresh-key]")].find((el) => el.dataset.refreshKey === state.focus);
    const control = target?.matches("details") ? target.querySelector("summary") : target;
    control?.focus({preventScroll:true});
  }
}
