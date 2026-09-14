// Small progressive enhancements: filter auto-submit, add-item tabs, live "similar items" lookup, photo preview.
(function () {
  // 1. filter toolbar: submit on change
  document.querySelectorAll("form[data-autosubmit] select").forEach(function (el) {
    el.addEventListener("change", function () { el.form.submit(); });
  });

  // 2. tabs (add item page)
  var tabs = document.querySelectorAll(".tabs a[data-tab]");
  if (tabs.length) {
    function activate(name) {
      tabs.forEach(function (t) { t.classList.toggle("active", t.dataset.tab === name); });
      document.querySelectorAll(".panel[data-panel]").forEach(function (p) { p.classList.toggle("active", p.dataset.panel === name); });
      try { history.replaceState(null, "", "?mode=" + name); } catch (e) {}
    }
    tabs.forEach(function (t) { t.addEventListener("click", function (ev) { ev.preventDefault(); activate(t.dataset.tab); }); });
  }

  // 3. live similar-items lookup while typing a name (debounced), rendered under the field
  var nameInput = document.querySelector("[data-similar]");
  if (nameInput) {
    var brandInput = document.querySelector("[data-similar-brand]");
    var box = document.getElementById(nameInput.dataset.similar);
    var excludeId = nameInput.dataset.exclude || "";
    var timer = null;
    function lookup() {
      var name = nameInput.value.trim();
      if (name.length < 3) { box.innerHTML = ""; return; }
      var params = new URLSearchParams({ name: name, brand: brandInput ? brandInput.value.trim() : "", exclude: excludeId });
      fetch("/api/items/similar?" + params.toString()).then(function (r) { return r.json(); }).then(function (data) {
        box.innerHTML = "";
        if (!data.matches || !data.matches.length) return;
        var head = document.createElement("div"); head.className = "help";
        head.textContent = data.matches[0].likely_duplicate ? "Looks like you already have this:" : "Similar items you already track:";
        box.appendChild(head);
        data.matches.forEach(function (m) {
          var a = document.createElement("a"); a.href = "/items/" + m.id; a.className = m.likely_duplicate ? "strong" : "";
          a.textContent = m.name + (m.category ? " · " + m.category : "");
          var s = document.createElement("span"); s.className = "score"; s.textContent = Math.round(m.score * 100) + "% match";
          a.appendChild(s); box.appendChild(a);
        });
      }).catch(function () {});
    }
    ["input", "change"].forEach(function (evt) {
      nameInput.addEventListener(evt, function () { clearTimeout(timer); timer = setTimeout(lookup, 300); });
      if (brandInput) brandInput.addEventListener(evt, function () { clearTimeout(timer); timer = setTimeout(lookup, 300); });
    });
    if (nameInput.value.trim().length >= 3) lookup();
  }

  // 4. photo preview
  var photo = document.querySelector("input[type=file][data-preview]");
  if (photo) {
    var img = document.getElementById(photo.dataset.preview);
    photo.addEventListener("change", function () {
      var f = photo.files && photo.files[0];
      if (!f || !img) return;
      var reader = new FileReader();
      reader.onload = function (e) { img.src = e.target.result; img.style.display = "block"; };
      reader.readAsDataURL(f);
    });
  }
})();
