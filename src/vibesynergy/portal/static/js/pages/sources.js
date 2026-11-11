// Sources (#/sources): logins, what each source collects and how often, and which source
// counts per metric. Built from the Settings page's parts (same saving, same restart banner).
import { render as settingsRender } from "./settings.js";

export function render(view, ctx) {
  return settingsRender(view, ctx, "sources");
}
