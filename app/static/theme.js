// Runs before first paint (loaded synchronously in <head>) so there is no light/dark flash.
(function () {
  var saved = null;
  try { saved = localStorage.getItem("tts.theme"); } catch (e) { /* storage blocked */ }
  var dark = saved ? saved === "dark" : window.matchMedia("(prefers-color-scheme: dark)").matches;
  document.documentElement.setAttribute("data-bs-theme", dark ? "dark" : "light");
})();
