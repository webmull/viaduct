#!/usr/bin/env python3
"""Bake the shared header and footer into every page as static markup.

This is the single source of truth for the site chrome. Edit the HEADER/FOOTER
markup below and re-run `python3 site/build.py`; it rewrites the region between
the BUILT markers in every site/**/*.html (or, on first run, replaces the old
`<div id="site-header"></div>` + `<script src="/header.js">` include).

Shipping the header/footer as static HTML (rather than injecting them with JS on
load) keeps the layout complete at first paint, so the browser's native scroll
restoration works without flashing the page to the top on reload.
"""
import pathlib
import re

SITE = pathlib.Path(__file__).resolve().parent

GH_ICON = (
    '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 2C6.477 2 2 6.477 2 12c0 4.419 2.865 8.17 6.839 9.49.5.092.682-.218.682-.486 0-.236-.009-.866-.014-1.7-2.782.605-3.369-1.343-3.369-1.343-.454-1.156-1.11-1.464-1.11-1.464-.908-.62.069-.608.069-.608 1.004.07 1.532 1.03 1.532 1.03.892 1.529 2.341 1.087 2.91.831.092-.646.35-1.086.636-1.337-2.22-.251-4.555-1.111-4.555-4.943 0-1.091.39-1.984 1.03-2.683-.103-.253-.447-1.27.098-2.647 0 0 .84-.269 2.75 1.025A9.578 9.578 0 0 1 12 6.836c.85.004 1.705.115 2.504.337 1.909-1.294 2.747-1.025 2.747-1.025.547 1.377.203 2.394.1 2.647.64.699 1.028 1.592 1.028 2.683 0 3.842-2.339 4.687-4.566 4.935.359.309.678.919.678 1.852 0 1.336-.012 2.415-.012 2.743 0 .267.18.578.688.48C19.138 20.167 22 16.418 22 12c0-5.523-4.477-10-10-10z"/></svg>'
)

HAMBURGER = (
    '<button class="nav-toggle" type="button" aria-label="Menu" aria-expanded="false" aria-controls="site-nav">'
    '<svg class="ico-open" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><line x1="3" y1="6" x2="21" y2="6"/><line x1="3" y1="12" x2="21" y2="12"/><line x1="3" y1="18" x2="21" y2="18"/></svg>'
    '<svg class="ico-close" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><line x1="6" y1="6" x2="18" y2="18"/><line x1="6" y1="18" x2="18" y2="6"/></svg>'
    "</button>"
)

# href -> (label, active-section). Section matches the leading path segment.
NAV = [
    ("/#how", "How it works", None),
    ("/#install", "Install", None),
    ("/network/", "Network", "network"),
    ("/docs/", "Docs", "docs"),
    ("/articles/", "Articles", "articles"),
    ("/about/", "About", "about"),
]


def header_html(active):
    links = ""
    for href, label, section in NAV:
        cur = ' aria-current="page"' if section and section == active else ""
        links += '<a class="nav-desktop" href="%s"%s>%s</a>' % (href, cur, label)
    return (
        '<header class="site-header has-menu"><div class="wrap header-inner">'
        '<a class="brand" href="/"><img src="/viaduct-mark.png" alt="" width="320" height="320" /><span>viaduct<span class="tld">.sh</span></span></a>'
        + HAMBURGER
        + '<nav class="site-nav" id="site-nav">'
        + links
        + '<a class="gh-pill" href="https://github.com/webmull/viaduct" target="_blank" rel="noreferrer">'
        + GH_ICON
        + "GitHub</a>"
        + "</nav></div></header>"
    )


FOOTER_HTML = (
    '<footer class="site-footer"><div class="wrap footer-inner">'
    '<a class="footer-cred" href="/about/">'
    '<img src="/adam.png" alt="Adam Davis" width="34" height="34" />'
    "<span>Built by <strong>Adam Davis</strong></span>"
    "</a>"
    '<div class="footer-links">'
    '<a href="https://github.com/webmull/viaduct" target="_blank" rel="noreferrer">GitHub</a>'
    '<a href="/network/">Network</a>'
    '<a href="/docs/">Docs</a>'
    '<a href="/compare/">Compare</a>'
    '<a href="/articles/">Articles</a>'
    '<a href="/terms/">Fair use</a>'
    '<a href="https://github.com/webmull/viaduct/blob/main/LICENSE" target="_blank" rel="noreferrer">MIT License</a>'
    "</div></div></footer>"
)

H_START, H_END = "<!-- BUILT header -->", "<!-- /BUILT header -->"
F_START, F_END = "<!-- BUILT footer -->", "<!-- /BUILT footer -->"


def active_for(rel):
    seg = rel.split("/", 1)[0]
    return seg if seg in ("network", "docs", "articles", "about") else None


def wrap(start, end, markup):
    return start + "\n" + markup + "\n" + end


def replace_region(text, start, end, first_pattern, markup):
    block = wrap(start, end, markup)
    marker_re = re.compile(re.escape(start) + r".*?" + re.escape(end), re.DOTALL)
    if marker_re.search(text):
        return marker_re.sub(lambda _: block, text, count=1)
    return re.sub(first_pattern, lambda _: block, text, count=1)


def main():
    header_first = re.compile(
        r'<div id="site-header"></div>\s*<script src="/header\.js[^"]*"></script>'
    )
    footer_first = re.compile(
        r'<div id="site-footer"></div>\s*<script src="/footer\.js[^"]*"></script>'
    )
    changed = 0
    for path in sorted(SITE.rglob("*.html")):
        rel = path.relative_to(SITE).as_posix()
        text = path.read_text(encoding="utf-8")
        orig = text
        text = replace_region(text, H_START, H_END, header_first, header_html(active_for(rel)))
        text = replace_region(text, F_START, F_END, footer_first, FOOTER_HTML)
        if text != orig:
            path.write_text(text, encoding="utf-8")
            changed += 1
            print("built", rel, "(nav:", active_for(rel) or "-", ")")
    print("done, %d files updated" % changed)


if __name__ == "__main__":
    main()
