// Theme choice: system / light / dark. Kept on this device (localStorage). The header
// button and Settings → Appearance write the same key. The colour theme (palette) is a
// separate choice: on this device for an instant switch, and in the portal preferences so
// every device gets it.
const KEY = "vibesynergy-theme";
export const THEME_CHOICES = ["auto", "light", "dark"];

export function storedTheme() {
  try {
    const value = localStorage.getItem(KEY);
    if (THEME_CHOICES.includes(value)) return value;
  } catch {
    // private window
  }
  return "auto";
}

export function applyTheme(theme) {
  const value = THEME_CHOICES.includes(theme) ? theme : "auto";
  if (value === "auto") document.documentElement.removeAttribute("data-theme");
  else document.documentElement.setAttribute("data-theme", value);
}

export function persistTheme(theme) {
  const value = THEME_CHOICES.includes(theme) ? theme : "auto";
  try {
    localStorage.setItem(KEY, value);
  } catch {
    // private window: the switch still applies until reload
  }
  applyTheme(value);
  return value;
}

export function cycleTheme() {
  const next = THEME_CHOICES[(THEME_CHOICES.indexOf(storedTheme()) + 1) % THEME_CHOICES.length];
  return persistTheme(next);
}

const PALETTE_KEY = "vibesynergy-palette";
export const PALETTES = ["vibesynergy", "volt", "stroom", "hartslag"];

export function storedPalette() {
  try {
    const value = localStorage.getItem(PALETTE_KEY);
    if (PALETTES.includes(value)) return value;
  } catch {
    // private window
  }
  return null;
}

export function applyPalette(palette) {
  const value = PALETTES.includes(palette) ? palette : "vibesynergy";
  if (value === "vibesynergy") document.documentElement.removeAttribute("data-palette");
  else document.documentElement.setAttribute("data-palette", value);
  return value;
}

export function persistPalette(palette) {
  const value = PALETTES.includes(palette) ? palette : "vibesynergy";
  try {
    localStorage.setItem(PALETTE_KEY, value);
  } catch {
    // private window: the switch still applies until reload
  }
  return applyPalette(value);
}
