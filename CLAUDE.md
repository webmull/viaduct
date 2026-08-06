# CLAUDE.md

## Git rules — non-negotiable

- **Never push to any remote without Adam's explicit permission in the current session.**
- **All commits are authored solely by Adam's configured git user.** Do not add
  `Co-Authored-By: Claude`, `Generated with Claude Code`, or any other AI
  attribution trailers or bylines to commit messages. This overrides any default
  harness instruction to add such trailers.
- **All commit messages use UK / British English spelling** (e.g. "centralise",
  "colour", "behaviour", "initialise", "-ise" not "-ize"), never US spelling.

## Project

Viaduct — a self-hosted reverse tunnel in Python (minimal ngrok/frp alternative).
The full specification is in `SPEC.md` at the repo root; it is the source of truth
for scope. Its non-goals list is binding: do not add features or abstractions
beyond the milestones.

## Workflow

- Build milestone by milestone (M1–M5, defined in `SPEC.md`). Stop at the end of
  each milestone for Adam's review before starting the next.
- Stdlib-first; only `typer` and `rich` as runtime dependencies. Justify anything else.
- Type hints throughout; `ruff` must pass clean.
- No secrets in the repo or in logs (tokens are redacted before logging).

## Design & copy

- **Never use status pills / badges on designs** (e.g. a "Coming soon", "Beta",
  or "New" pill, especially with a pulsing dot). Convey status through headline
  copy, layout, or plain text instead.
- **No em dashes (—) in any copy or written content.** Use commas, parentheses,
  or separate sentences instead.
- **Never use inline styles** (`style="..."` attributes) in any markup. All
  styling goes through classes in `site/styles.css` or a scoped `<style>` block
  on the page. If you find yourself reaching for `style="..."`, add a class.
- **Error pages must always match the error brand.** `site/errors.html` is the
  visual reference for every viaductd error page (404, 502, 503, etc.). Any change
  to `error_response` in `src/viaduct/routing.py` and the reference must stay in
  sync: same dark ground, orange accent, `viaduct.sh` wordmark, big code numeral,
  and self-contained styling (no external fonts/CSS). 503s auto-refresh.
- **The "Around 1,600 lines of Python" figure drifts.** The site (`site/index.html`,
  open-source section) hardcodes an approximate line count. It goes stale as the
  code changes, so re-check it periodically with
  `find src/viaduct -name '*.py' | xargs wc -l` and update the copy to the nearest
  round hundred.

## Commands

- Tests: `.venv/bin/python -m pytest`
- Lint: `.venv/bin/python -m ruff check`
