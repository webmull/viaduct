(function () {
  var header = document.querySelector(".site-header.has-menu");
  if (!header) return;
  var btn = header.querySelector(".nav-toggle");
  if (!btn) return;

  function close() {
    header.classList.remove("open");
    btn.setAttribute("aria-expanded", "false");
  }

  btn.addEventListener("click", function () {
    var open = header.classList.toggle("open");
    btn.setAttribute("aria-expanded", open ? "true" : "false");
  });

  // close after picking a destination, and on Escape
  header.querySelectorAll(".site-nav a").forEach(function (a) {
    a.addEventListener("click", close);
  });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") close();
  });
})();
