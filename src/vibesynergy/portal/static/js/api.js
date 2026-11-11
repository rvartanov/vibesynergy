// Calls to the portal's JSON API. Every response is {data, meta}; errors carry {error}.

import { language } from "./i18n.js";

export class ApiError extends Error {
  constructor(message, code) {
    super(message);
    this.code = code; // a stable error code, where the server sends one
  }
}

// The latest failed calls (kept for debugging in the console).
const failed = [];
function note(method, path, status, error) {
  failed.unshift({ method, url: path, status, error: String(error).slice(0, 300), at: new Date().toISOString() });
  failed.length = Math.min(failed.length, 20);
}

export async function call(method, path, doFetch) {
  let response;
  try {
    response = await doFetch();
  } catch (error) {
    note(method, path, "network", error.message);
    throw error;
  }
  try {
    return await parse(response);
  } catch (error) {
    note(method, path, response.status, error.message);
    throw error;
  }
}

export function url(path, params = {}) {
  const query = new URLSearchParams();
  // The server writes some sentences itself (the Dashboard's advice, injury warnings).
  if (language() !== "en") query.append("lang", language());
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === "") continue;
    for (const v of Array.isArray(value) ? value : [value]) query.append(key, v);
  }
  const qs = query.toString();
  // Relative, so the portal also works below a path prefix (shared links).
  return `api/${path}${qs ? `?${qs}` : ""}`;
}

async function parse(response) {
  let body = null;
  try {
    body = await response.json();
  } catch {
    // not JSON: fall through to the status message
  }
  if (!response.ok) {
    throw new ApiError(body?.error || `${response.status} ${response.statusText}`, body?.code);
  }
  return body;
}

export async function get(path, params, options = {}) {
  const target = url(path, params);
  const controller = options.timeoutMs ? new AbortController() : null;
  const timer = controller ? setTimeout(() => controller.abort(), options.timeoutMs) : null;
  try {
    return await call("GET", target, () =>
      fetch(target, {
        cache: "no-store",
        headers: { accept: "application/json" },
        signal: options.signal || controller?.signal,
      }),
    );
  } finally {
    if (timer) clearTimeout(timer);
  }
}

// Writes carry the X-VibeSynergy header; the server refuses writes without it, which stops
// other web pages from triggering actions here.
export async function post(path, body = {}) {
  return call("POST", url(path), () =>
    fetch(url(path), {
      method: "POST",
      headers: { "content-type": "application/json", "x-vibesynergy": "portal" },
      body: JSON.stringify(body),
    }),
  );
}

// Today's date, units, sources, the metric catalogue. The age cap follows the page
// refresh setting (at most five minutes) so a one-minute dashboard reload does not keep
// yesterday's date. Callers also drop the cache when the calendar day changes.
const META_MAX_AGE_MS = 5 * 60 * 1000;
let metaMaxAgeMs = META_MAX_AGE_MS;
let metaPromise = null;
let metaAt = 0;

export function setMetaMaxAge(ms) {
  metaMaxAgeMs = ms > 0 ? ms : META_MAX_AGE_MS;
}

export function invalidateMeta() {
  metaPromise = null;
  metaAt = 0;
}

export function meta() {
  if (!metaPromise || Date.now() - metaAt > metaMaxAgeMs) {
    metaAt = Date.now();
    metaPromise = get("meta").catch((error) => {
      metaPromise = null;
      throw error;
    });
  }
  return metaPromise;
}

// File upload (multipart), with the same write header as post().
export async function upload(path, files) {
  const form = new FormData();
  for (const file of files) form.append("files", file, file.name);
  return call("POST", url(path), () => fetch(url(path), { method: "POST", headers: { "x-vibesynergy": "portal" }, body: form }));
}

