// Hover layer for .viz-line-chart: a crosshair snaps to the nearest check date and one tooltip lists every series.
(function () {
  function setup(figure) {
    var svg = figure.querySelector("svg");
    var dataEl = figure.querySelector(".viz-data");
    var tip = figure.querySelector(".viz-tooltip");
    if (!svg || !dataEl || !tip) return;
    var data = JSON.parse(dataEl.textContent);
    var cross = svg.querySelector(".viz-crosshair");
    var vb = svg.viewBox.baseVal;

    function nearest(px) {
      var best = 0, bestDist = Infinity;
      data.xs.forEach(function (x, i) { var d = Math.abs(x - px); if (d < bestDist) { bestDist = d; best = i; } });
      return best;
    }

    svg.addEventListener("pointermove", function (ev) {
      var rect = svg.getBoundingClientRect();
      var px = (ev.clientX - rect.left) / rect.width * vb.width;
      if (px < data.left - 10 || px > data.right + 10) { hide(); return; }
      var i = nearest(px);
      var x = data.xs[i];
      cross.setAttribute("x1", x); cross.setAttribute("x2", x); cross.style.opacity = "1";
      while (tip.firstChild) tip.removeChild(tip.firstChild);
      var head = document.createElement("div"); head.className = "viz-tip-head"; head.textContent = data.labels[i]; tip.appendChild(head);
      data.series.forEach(function (s) {
        var value = s.values[data.labels[i]];
        if (value === undefined) return;
        var row = document.createElement("div"); row.className = "viz-tip-row";
        var key = document.createElement("i"); key.className = s.cls; row.appendChild(key);
        var strong = document.createElement("strong"); strong.textContent = value; row.appendChild(strong);
        var name = document.createElement("span"); name.textContent = " " + s.name; row.appendChild(name);
        tip.appendChild(row);
      });
      tip.hidden = false;
      var fx = (x / vb.width) * rect.width;
      tip.style.left = Math.min(fx + 12, rect.width - tip.offsetWidth - 4) + "px";
      tip.style.top = "28px";
    });
    function hide() { cross.style.opacity = "0"; tip.hidden = true; }
    svg.addEventListener("pointerleave", hide);
  }
  document.querySelectorAll(".viz-line-chart").forEach(setup);
})();
