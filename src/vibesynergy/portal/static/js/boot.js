// Classic (non-module) script: it still runs when app.js or one of its imports fails to load
// or is too new for the browser, which would otherwise leave the page empty without a word.
// Module scripts have run (or failed) before the load event.
window.addEventListener("load", () => {
  if (window.vibesynergyStarted) return;
  const view = document.getElementById("view");
  if (!view || view.textContent.trim()) return;
  const dutch = (navigator.language || "").toLowerCase().startsWith("nl");
  view.innerHTML = dutch
    ? '<div class="notice error">Het portaal startte niet in deze browser. Vernieuw de pagina; ' +
      "blijft het leeg, werk de browser bij (Safari 16.4 of nieuwer, of een recente Chrome, Firefox of Edge).</div>"
    : '<div class="notice error">The portal could not start in this browser. Reload the page; ' +
      "if it stays empty, update the browser (Safari 16.4 or newer, or a recent Chrome, Firefox or Edge).</div>";
});
