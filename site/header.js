// Single source of truth for the site header. Each page has just
// `<div id="site-header"></div>` followed by this script (loaded synchronously,
// so the header is in place before first paint); the markup lives here once so
// the nav stays identical on every page. The current page is marked with
// aria-current based on the path.
(function () {
  // Turn off the browser's automatic scroll restoration. On iOS Safari it
  // paints the page at the top on reload and then jumps to the saved position
  // once layout settles, which reads as a flash. With this off, a reload just
  // stays at the top: nothing to jump to.
  if ("scrollRestoration" in history) {
    try { history.scrollRestoration = "manual"; } catch (e) {}
  }

  var mount = document.getElementById("site-header");
  if (!mount) return;

  var path = location.pathname;
  var startsWith = function (prefix) {
    return path === prefix || path.indexOf(prefix) === 0;
  };

  // Canonical nav, matching the homepage. Anchors are absolute (/#how) so they
  // work from every page, including the homepage itself.
  var nav = [
    { href: "/#how", label: "How it works" },
    { href: "/#install", label: "Install" },
    { href: "/network/", label: "Network", active: startsWith("/network/") },
    { href: "/docs/", label: "Docs", active: startsWith("/docs/") },
    { href: "/news/", label: "News", active: startsWith("/news/") },
    { href: "/about/", label: "About", active: startsWith("/about/") },
  ];

  var githubIcon =
    '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 2C6.477 2 2 6.477 2 12c0 4.419 2.865 8.17 6.839 9.49.5.092.682-.218.682-.486 0-.236-.009-.866-.014-1.7-2.782.605-3.369-1.343-3.369-1.343-.454-1.156-1.11-1.464-1.11-1.464-.908-.62.069-.608.069-.608 1.004.07 1.532 1.03 1.532 1.03.892 1.529 2.341 1.087 2.91.831.092-.646.35-1.086.636-1.337-2.22-.251-4.555-1.111-4.555-4.943 0-1.091.39-1.984 1.03-2.683-.103-.253-.447-1.27.098-2.647 0 0 .84-.269 2.75 1.025A9.578 9.578 0 0 1 12 6.836c.85.004 1.705.115 2.504.337 1.909-1.294 2.747-1.025 2.747-1.025.547 1.377.203 2.394.1 2.647.64.699 1.028 1.592 1.028 2.683 0 3.842-2.339 4.687-4.566 4.935.359.309.678.919.678 1.852 0 1.336-.012 2.415-.012 2.743 0 .267.18.578.688.48C19.138 20.167 22 16.418 22 12c0-5.523-4.477-10-10-10z"/></svg>';

  var hamburger =
    '<button class="nav-toggle" type="button" aria-label="Menu" aria-expanded="false" aria-controls="site-nav">' +
    '<svg class="ico-open" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><line x1="3" y1="6" x2="21" y2="6"/><line x1="3" y1="12" x2="21" y2="12"/><line x1="3" y1="18" x2="21" y2="18"/></svg>' +
    '<svg class="ico-close" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><line x1="6" y1="6" x2="18" y2="18"/><line x1="6" y1="18" x2="18" y2="6"/></svg>' +
    "</button>";

  var links = nav
    .map(function (n) {
      return (
        '<a class="nav-desktop" href="' +
        n.href +
        '"' +
        (n.active ? ' aria-current="page"' : "") +
        ">" +
        n.label +
        "</a>"
      );
    })
    .join("");

  mount.outerHTML =
    '<header class="site-header has-menu"><div class="wrap header-inner">' +
    '<a class="brand" href="/"><img src="/viaduct-mark.png" alt="" width="320" height="320" /><span>viaduct<span class="tld">.sh</span></span></a>' +
    hamburger +
    '<nav class="site-nav" id="site-nav">' +
    links +
    '<a class="gh-pill" href="https://github.com/webmull/viaduct" target="_blank" rel="noreferrer">' +
    githubIcon +
    "GitHub</a>" +
    "</nav></div></header>";
})();
