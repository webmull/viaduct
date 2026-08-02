(function () {
  var btn = document.createElement("button");
  btn.className = "to-top";
  btn.type = "button";
  btn.setAttribute("aria-label", "Back to top");
  btn.title = "Back to top";
  btn.innerHTML =
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 19V6"/><path d="M6 12l6-6 6 6"/></svg>';
  document.body.appendChild(btn);

  var reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  var footer = document.querySelector(".site-footer");
  var BASE = 18; // ~1.15rem resting offset from the bottom

  function update() {
    btn.classList.toggle("show", window.scrollY > 420);
    // dock above the footer once it scrolls into view, so it never overlaps
    var lift = 0;
    if (footer) {
      var top = footer.getBoundingClientRect().top;
      lift = Math.max(0, window.innerHeight - top + 12);
    }
    btn.style.bottom = BASE + lift + "px";
  }
  window.addEventListener("scroll", update, { passive: true });
  window.addEventListener("resize", update);
  update();

  btn.addEventListener("click", function () {
    window.scrollTo({ top: 0, behavior: reduce ? "auto" : "smooth" });
  });
})();
