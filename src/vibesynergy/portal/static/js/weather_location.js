// Settings → System → Weather on the calendar: where the forecast for the coming days is for.
// By default where you last trained outside; or a place you look up by name. Saves at once.
import { get, post } from "./api.js";
import { esc } from "./format.js";
import { language, t } from "./i18n.js";

function cardHtml(chosen) {
  return `<h2 id="weather-location-title">${esc(t("Weather on the calendar"))}</h2>
    <p class="sub">${esc(t("The forecast for the coming days on the calendar and for your coach."))}</p>
    <fieldset class="settings-field">
      <legend class="visually-hidden">${esc(t("Weather on the calendar"))}</legend>
      <label class="check"><input type="radio" name="weather-where" value="last"${chosen ? "" : " checked"}> ${esc(t("Where I last trained outside"))}</label>
      <label class="check"><input type="radio" name="weather-where" value="place"${chosen ? " checked" : ""}> ${esc(chosen ? t("Fixed place: {name}", { name: chosen.name }) : t("A fixed place"))}</label>
    </fieldset>
    <div class="settings-field" data-weather-search${chosen ? "" : " hidden"}>
      <label for="weather-place">${esc(t("Look up a place"))}</label>
      <div class="settings-save">
        <input class="btn" id="weather-place" type="search" autocomplete="off" placeholder="${esc(t("e.g. Stad"))}">
        <button class="btn" type="button" data-weather-find>${esc(t("Search"))}</button>
      </div>
      <ul class="trends" data-weather-results></ul>
    </div>
    <p class="sub" role="status" aria-live="polite" data-weather-status></p>`;
}

export async function weatherLocationSettings(el, stale) {
  const { data } = await get("prefs");
  if (stale() || !el.isConnected) return;
  draw(el, stale, data.weather_location || null);
}

function draw(el, stale, chosen) {
  el.innerHTML = cardHtml(chosen);
  const status = el.querySelector("[data-weather-status]");
  const search = el.querySelector("[data-weather-search]");
  const results = el.querySelector("[data-weather-results]");
  const input = el.querySelector("#weather-place");
  const save = async (value, done) => {
    try {
      await post("prefs", { changes: { weather_location: value } });
      if (stale() || !el.isConnected) return;
      draw(el, stale, value);
      el.querySelector("[data-weather-status]").textContent = done;
    } catch (error) {
      status.textContent = t("Not saved: {error}", { error: error.message });
    }
  };
  for (const radio of el.querySelectorAll('[name="weather-where"]')) {
    radio.addEventListener("change", () => {
      if (radio.value === "last") {
        if (chosen) save(null, t("Saved. The forecast follows where you last trained outside."));
        search.hidden = true;
      } else {
        search.hidden = false;
        input.focus();
      }
    });
  }
  const find = async () => {
    const q = input.value.trim();
    if (q.length < 2) {
      status.textContent = t("Type at least two letters.");
      return;
    }
    status.textContent = t("Searching…");
    const { data } = await get("weather/places", { q, lang: language() === "en" ? "en" : "nl" });
    if (stale() || !el.isConnected) return;
    const found = data.places || [];
    results.innerHTML = found.map((p, i) => `<li><button class="btn" type="button" data-weather-pick="${i}">${esc(p.name)}</button>${p.where ? ` <span class="sub">${esc(p.where)}</span>` : ""}</li>`).join("");
    status.textContent = found.length ? t("{n} places found. Choose one.", { n: found.length }) : t("No place found. Try another spelling.");
    for (const button of results.querySelectorAll("[data-weather-pick]")) {
      button.addEventListener("click", () => {
        const p = found[Number(button.dataset.weatherPick)];
        const name = p.where ? `${p.name}, ${p.where}` : p.name;
        save({ name, latitude: p.latitude, longitude: p.longitude }, t("Saved. The forecast is now for {name}.", { name }));
      });
    }
  };
  el.querySelector("[data-weather-find]").addEventListener("click", () => find().catch((error) => { status.textContent = error.message; }));
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      find().catch((error) => { status.textContent = error.message; });
    }
  });
}
