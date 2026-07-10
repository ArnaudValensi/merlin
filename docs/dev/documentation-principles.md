# Documentation Principles

How Merlin's documentation is structured and written. This is the contract
for anyone (human or agent) writing or updating docs, and the rulebook a
future doc-maintenance skill applies when reconciling docs against the
code.

## Surfaces and audiences

One concept has exactly one authoritative prose home; every other surface
links to it instead of restating it.

| Surface | Owns | Audience |
|---|---|---|
| `README.md` | the pitch, the flywheel overview, and the complete index of user docs | a visitor deciding whether to install |
| `docs/*.md` (top level) | task guides: what each feature is for and how to use it | users |
| `docs/dev/*.md` | implementation: how it is built | contributors |
| `merlin <cmd> --help` | exact syntax and options | anyone, terse |
| `agent/MERLIN.md` | the agent operating model, compressed (injected per run) | agents |

Consequences:

- Syntax lives only in `--help`. Docs link to it and never restate flag
  lists (they drift).
- The README gives the main overview of how Merlin works, then links out
  for every detail. Nothing user-facing may be reachable only through
  `--help` or a dev doc: every feature has a user doc linked from the
  README, directly or one hop away.
- The brain doc stays small and points to user docs for depth.

## Structure: one doc per page

The dashboard is the product surface, so user docs mirror it: one doc per
Merlin page (`terminal.md`, `files.md`, `commits.md`, `notes.md`,
`jobs.md`, `bot.md`, `extensions.md`), plus the non-page guides
(`getting-started.md`, `agents.md`, `creating-extensions.md`, `cli.md`).
`cli.md` is the command map: what each command family is for, syntax
deferred to `--help` per the single-source rule.

Voice input is folded into the docs of the surfaces that have it
(`terminal.md`, `bot.md`), not a doc of its own.

## The page-doc template

Every page doc follows the same shape, in this order:

1. **What it is**: one short paragraph, then a screenshot. State which
   node of the flywheel this page is and what it feeds (the work loop,
   the memory, the agent).
2. **Tasks**: "do X" sections covering everything the page can do.
3. **Mobile notes**: anything phone-specific (gestures, toolbar, quirks).
4. **Troubleshooting**: the real failure modes and their fixes.

## Implementation details in user docs

Allowed only when they explain behavior the user experiences. Litmus
test: tmux persistence belongs in `terminal.md` (it explains why your
session survives closing the tab); the WebSocket/PTY plumbing and escape
sequence tables do not (they go to `docs/dev/`).

## Screenshots

- Mobile-first (~390px portrait); that is the product's story.
- Reuse the landing page shots (`portal/static/img/phone-*.jpg` in the
  merlin-saas repo) when one fits; they are curated and already public.
- New captures are taken on a throwaway instance (temp `MERLIN_HOME`,
  separate port) so no real user data leaks, and every image is reviewed
  before it is embedded.
- When a shot cannot be taken safely or needs real channels (a Discord
  exchange, a live terminal session), leave a
  `[SCREENSHOT PLACEHOLDER (...)]` with a precise description of the shot
  to take.
- Images live in a per-doc asset folder named after the doc
  (`docs/<doc-name>/*.png|jpg`). When the same shot fits several docs,
  embed the existing file from its home folder instead of duplicating
  the binary.

## Voice and tone

- Geeky and terminal-native, not polished-SaaS marketing.
- No em dashes. Use colons or periods.
- Address the reader as "you". Say "your agent" for the AI (Merlin is the
  tool suite around the user's agent, not an AI itself).
- Public-facing terms only: say "Merlin Cloud", never "the portal".
- The bundled Cloudflare tunnel is removed: docs describe remote access
  as bring-your-own tunnel/reverse proxy or Merlin Cloud, and never
  present a bundled tunnel as a current feature.

## Verification

- The link-checker unit test (`tests/unit/test_doc_links.py`) must stay
  green; run `uv run scripts.py validate` after every docs commit.
- Every command shown is actually run and its real output used; no
  invented output. A runnable guide is its own test: build what it says,
  exactly as written (this is how `creating-extensions.md` is validated).
- Every factual claim about behavior is verified against the code before
  it lands, and re-verified by the doc-maintenance pass when code in the
  corresponding module changes.

## Maintenance (the future doc skill)

A periodic doc pass applies this file mechanically:

1. Diff each page doc against its module (template above; facts vs code).
2. Re-run the link checker and the command transcripts.
3. Flag drift (flags, paths, renamed pages, new features without a doc,
   docs for removed features) rather than silently rewriting; propose
   edits per the single-source table.
