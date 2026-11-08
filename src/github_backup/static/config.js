(() => {
  "use strict";
  const choices = new Set(["system", "light", "dark"]);
  const select = document.getElementById("theme");
  let choice = "system";
  try { const stored = localStorage.getItem("github-backup-theme"); if (choices.has(stored)) choice = stored; } catch (_) {}
  function apply(value) {
    if (!choices.has(value)) return;
    if (value === "system") document.documentElement.removeAttribute("data-theme");
    else document.documentElement.setAttribute("data-theme", value);
  }
  apply(choice);
  if (select) {
    select.value = choice;
    select.addEventListener("change", () => {
      apply(select.value);
      try { localStorage.setItem("github-backup-theme", select.value); } catch (_) {}
    });
  }
  document.querySelectorAll("time[data-local]").forEach((element) => {
    const date = new Date(element.getAttribute("datetime"));
    if (Number.isNaN(date.getTime())) return;
    element.textContent = new Intl.DateTimeFormat(undefined, {dateStyle: "medium", timeStyle: "short"}).format(date);
  });
  const timezone = document.getElementById("timezone");
  if (timezone) timezone.textContent = "Times shown in " + Intl.DateTimeFormat().resolvedOptions().timeZone + ".";
})();
(() => {
  const sections = new Set(["storage", "github", "scope", "monitoring"]);
  function openSection(hash) {
    const id = hash.slice(1);
    if (!sections.has(id)) return;
    const section = document.getElementById(id);
    if (section instanceof HTMLDetailsElement) section.open = true;
  }
  openSection(window.location.hash);
  document.querySelectorAll(".steps a").forEach((link) => {
    link.addEventListener("click", () => openSection(link.hash));
  });
})();
