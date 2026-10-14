// Small progressive enhancements. Every page works without them, except the live similar-items hint and the mic.
"use strict";
(() => {
  // Filter toolbar: submit on change.
  document.querySelectorAll("form[data-autosubmit] select").forEach((el) => el.addEventListener("change", () => el.form.submit()));

  // Forms that need a yes first (delete, clear). Inline handlers are not allowed by the content security policy.
  document.querySelectorAll("form[data-confirm]").forEach((form) =>
    form.addEventListener("submit", (e) => { if (!confirm(form.dataset.confirm)) e.preventDefault(); }));

  // Forms that wait for Claude: say so, and do not send twice.
  document.querySelectorAll("form[data-busy]").forEach((form) =>
    form.addEventListener("submit", () => {
      if (form.classList.contains("busy")) return;
      form.classList.add("busy");
      const note = document.createElement("span");
      note.className = "busy-note";
      note.textContent = form.dataset.busy;
      form.querySelector("button[type=submit]")?.after(note);
    }));

  // Tabs (add item page).
  const tabs = document.querySelectorAll(".tabs a[data-tab]");
  tabs.forEach((t) => t.addEventListener("click", (e) => {
    e.preventDefault();
    tabs.forEach((x) => x.classList.toggle("active", x === t));
    document.querySelectorAll(".panel[data-panel]").forEach((p) => p.classList.toggle("active", p.dataset.panel === t.dataset.tab));
    try { history.replaceState(null, "", "?mode=" + t.dataset.tab); } catch (err) { /* not remembered */ }
  }));

  // Similar items you already watch, under the name field while typing.
  const nameInput = document.querySelector("[data-similar]");
  if (nameInput) {
    const brandInput = document.querySelector("[data-similar-brand]");
    const box = document.getElementById(nameInput.dataset.similar);
    let timer = null;
    const lookup = () => {
      const name = nameInput.value.trim();
      if (name.length < 3) { box.replaceChildren(); return; }
      const params = new URLSearchParams({ name, brand: brandInput ? brandInput.value.trim() : "", exclude: nameInput.dataset.exclude || "" });
      fetch("/api/items/similar?" + params).then((r) => r.json()).then((data) => {
        box.replaceChildren();
        if (!data.matches || !data.matches.length) return;
        const head = document.createElement("div");
        head.className = "help";
        head.textContent = data.matches[0].likely_duplicate ? "Looks like you already have this:" : "Similar items you already watch:";
        box.append(head);
        data.matches.forEach((m) => {
          const a = document.createElement("a");
          a.href = "/items/" + m.id;
          a.className = m.likely_duplicate ? "strong" : "";
          a.textContent = m.name + (m.category ? " · " + m.category : "");
          const s = document.createElement("span");
          s.className = "score";
          s.textContent = Math.round(m.score * 100) + "% match";
          a.append(s);
          box.append(a);
        });
      }).catch(() => {});
    };
    const later = () => { clearTimeout(timer); timer = setTimeout(lookup, 300); };
    [nameInput, brandInput].filter(Boolean).forEach((el) => ["input", "change"].forEach((evt) => el.addEventListener(evt, later)));
    if (nameInput.value.trim().length >= 3) lookup();
  }

  // Photo preview.
  document.querySelectorAll("input[type=file][data-preview]").forEach((input) => input.addEventListener("change", () => {
    const img = document.getElementById(input.dataset.preview), file = input.files && input.files[0];
    if (!img || !file) return;
    img.src = URL.createObjectURL(file);
    img.style.display = "block";
  }));

  // Voice: speech becomes text in the browser (Web Speech API; needs HTTPS). Only the text is sent, after you
  // read it. Tap the mic to start, tap again to stop. Hidden where the browser has no speech recognition; the
  // keyboard's own dictation key works there.
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (SR) document.querySelectorAll("[data-voice]").forEach((button) => {
    const field = document.getElementById(button.dataset.voice);
    const status = button.closest(".talk")?.querySelector(".voice-status");
    const say = (text) => { if (status) status.textContent = text; };
    let rec = null;
    button.hidden = false;
    button.addEventListener("click", () => {
      if (rec) { rec.stop(); return; }
      const before = field.value.trim();
      rec = new SR();
      rec.lang = navigator.language || "en-GB";
      rec.interimResults = true;
      rec.continuous = true;
      rec.onresult = (e) => {
        const said = Array.from(e.results, (r) => r[0].transcript).join(" ").trim();
        field.value = (before && said ? before + " " : before) + said;
      };
      rec.onerror = (e) => say(e.error === "not-allowed" || e.error === "service-not-allowed"
        ? "The microphone is blocked here. Use the microphone key on your keyboard instead."
        : "Did not catch that. Tap the mic to try again.");
      rec.onend = () => {
        rec = null;
        button.classList.remove("listening");
        if (field.value.trim()) say("Stopped. Edit it if you like, then send.");
      };
      try { rec.start(); } catch (err) { rec = null; say("The microphone could not start. Try again."); return; }
      button.classList.add("listening");
      say("Listening. Tap the mic again to stop.");
    });
  });

  // Ask: an example fills the box.
  document.querySelectorAll("[data-example]").forEach((li) => li.addEventListener("click", () => {
    const box = document.getElementById("question");
    if (box) { box.value = li.textContent; box.focus(); }
  }));

  // A select with data-remember keeps its choice on this device (the model picker on Ask).
  document.querySelectorAll("select[data-remember]").forEach((select) => {
    const key = select.dataset.remember;
    try {
      const kept = localStorage.getItem(key);
      if (kept && [...select.options].some((o) => o.value === kept)) select.value = kept;
    } catch (err) { /* private mode: the default stays */ }
    select.addEventListener("change", () => { try { localStorage.setItem(key, select.value); } catch (err) { /* not kept */ } });
  });

  // The latest answer on Ask comes into view.
  document.getElementById("latest")?.scrollIntoView({ block: "nearest" });

  // Caches the icons only, so the app can be added to the Home Screen (needs HTTPS or localhost).
  if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});
})();
