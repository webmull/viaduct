// Single source of truth for the site footer. Each page has just
// `<div id="site-footer"></div>` followed by this script; the markup lives here
// once so the footer stays identical on every page.
(function () {
  var mount = document.getElementById("site-footer");
  if (!mount) return;
  mount.outerHTML =
    '<footer class="site-footer"><div class="wrap footer-inner">' +
    '<a href="/about/" style="display:flex;align-items:center;gap:0.6rem;text-decoration:none;color:inherit">' +
    '<img src="/adam.png" alt="Adam Davis" width="34" height="34" style="border-radius:999px;border:1px solid var(--border-strong)" />' +
    "<span>Built by <strong style=\"color:var(--foreground);font-weight:600\">Adam Davis</strong></span>" +
    "</a>" +
    '<div class="footer-links">' +
    '<a href="https://github.com/webmull/viaduct" target="_blank" rel="noreferrer">GitHub</a>' +
    '<a href="/network/">Network</a>' +
    '<a href="/docs/">Docs</a>' +
    '<a href="/news/">News</a>' +
    '<a href="/terms/">Fair use</a>' +
    '<a href="https://github.com/webmull/viaduct/blob/main/LICENSE" target="_blank" rel="noreferrer">MIT License</a>' +
    "</div></div></footer>";
})();
