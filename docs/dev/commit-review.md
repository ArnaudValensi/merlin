# Commit review: comparisons, reviews, comments

Developer reference for the review side of the Commits page: the
`base..head` comparison model behind every view, how refs from user input
reach git safely, the routes, and (as they land) the saved reviews, the
viewed-file rule, comment re-anchoring and the `merlin review` CLI. The
user-facing description is [`docs/commits.md`](../commits.md).

> Code: `commits/compare.py` (the model), `commits/git_parser.py` (git
> wrappers and parsers), `commits/routes.py` (pages and API),
> `commits/static/compare-model.js` (pure UI model, node-tested),
> `commits/static/commits.js` (the page). Tests: `tests/unit/test_compare.py`
> (on a temporary repository), `tests/unit/test_commits_routes.py` (mocked
> git), `tests/js/compare-model.test.js`, `tests/e2e/test_commit_compare.py`.

## The comparison model

Every diff the page shows is a comparison of two points of the repository,
`base..head`:

| Kind | base | head | How it is picked |
|------|------|------|------------------|
| `commit` | `commit^` (the empty tree for a root commit) | `commit` | Tap a row, or a Select-mode range of one |
| `range` | `oldest^` | `newest` | Select mode, two taps in the list |
| `branch` | `merge-base(base, head)` | `head` | The Compare sheet with **Against merge base** on |
| `worktree` | `HEAD` (or a typed base) | the files on disk | The pinned Working tree row or the sheet's shortcut |

`resolve_comparison()` turns the query (`base`, `head`, `mergebase`,
`worktree`) into a `Comparison` record: the refs as typed, `base_resolved`
and `head_resolved` (shas), `merge_base` when asked for (recomputed on every
load, so a base branch that advances is followed), and `diff_base`, the sha
every diff runs against. `head_resolved` is null for the working tree, which
has no head commit. The kind is derived: `worktree`, `branch` when the merge
base is used, `commit` when `diff_base` is the head's first parent (or the
empty tree under a root commit), `range` otherwise.

A `<sha>^` base whose commit is the repository's root does not exist in git.
`_resolve_base` maps it to git's empty tree
(`4b825dc642cb6eb9a060e54bf8d69288fbee4904`), so a range or a single view
that starts at the root commit works like any other. A `^` on a ref that
does not resolve at all stays an error.

Three functions produce what the page renders, for any comparison:

- `compare_files(cmp)`: `git diff --numstat` and `--name-status` between
  `diff_base` and the head, parsed exactly as the single-commit detail always
  was (same payload, rename quirks included). For the working tree,
  `git diff <base>` (staged and unstaged together) plus every untracked,
  non-ignored file from `git ls-files --others --exclude-standard`, shown as
  added with its line count (0 when binary by git's NUL heuristic).
- `compare_diff(cmp)`: `git diff -p` parsed by `_parse_unified_diff` into
  `FileDiff` records. Untracked files get a diff from
  `git diff --no-index -- /dev/null <path>`, appended to the same text.
- `compare_file(cmp, path)`: the full file at the head, with gutters from
  `git diff diff_base head -- path`. The blob is named by `git ls-tree
  --end-of-options <head> -- <path>` and read by `git cat-file blob <oid>`:
  the user's path is never embedded in a revision argument like
  `<sha>:<path>`. For the working tree the file is read from disk after
  `worktree_file_path` resolves it (symlinks followed) and checks it is
  inside the repository root.

Every read from disk goes through one gate, `_contained_file`: the path is
resolved with symlinks followed and must land on a regular file inside the
resolved root. The full-file view refuses anything else (a 400 for a path
that escapes, a 404 for a missing path, a directory, a FIFO). The untracked
entries of the working-tree list and diff go through the same gate: an
untracked symlink that escapes the root or a dangling link is still listed
as added, with no line count and an empty diff, and is never opened (git
itself does not list a FIFO or a device under `--others`, and a direct read
of one is a 404). Line counts are streamed in 1 MB chunks so a large
untracked file never lands in memory at once.

`compare_commits(cmp)` lists `diff_base..head` (newest first, at most 200),
counts it from one `rev-list`, and returns the oldest included commit
separately, because the list is capped and the range's start must not be
read from a truncated list. `compare_detail(cmp)` is the payload of
`GET /api/commits/compare`: the record's fields plus short hashes
(`base_short`, `head_short`, `merge_base_short`, `diff_base_short`), the
commits, the count, `oldest` and the files.

The single-commit routes are the special case: `get_commit_detail`,
`get_commit_diff` and `get_file_with_gutters` in `compare.py` build a
`commit_comparison(hash)` (`commit^..commit`) and call the three functions
above. Their paths and payloads are unchanged (the meta line still comes from
`git show --no-patch`).

## Ref safety

User input reaches git as something other than a hex hash for the first time
here. The rules, in `compare.py`:

1. **Shape.** `validate_ref` accepts what `REF_RE` fully matches,
   `[A-Za-z0-9][A-Za-z0-9._/+^~@{}-]*`, and refuses an empty string, a leading
   `-`, and any `..`. Branch names with slashes, tags, `HEAD~3`, `abc123^`,
   `origin/main`, `main@{upstream}` and `HEAD^{/text}` all pass. Spaces, `;`,
   `|`, `$`, newlines and option-looking strings never reach git. The regex is
   applied with `fullmatch`: `$` would accept a trailing newline.
2. **Resolve first.** `resolve_ref` runs `git rev-parse --verify --quiet
   --end-of-options <ref>^{commit}` before any other command and raises
   `RefError` (a 400 in the routes) when it fails. Every later git call takes
   the resolved sha, never the typed string.
3. **`--end-of-options` and `--`.** Every git invocation built from user
   input puts `--end-of-options` before refs and `--` before paths, including
   the calls that only ever see resolved shas. `tests/unit/test_compare.py`
   spies on `_run_git` across a full comparison and asserts it.
4. **Working-tree reads stay inside the root.** `_contained_file` resolves
   `root / path` with symlinks followed and accepts only a regular file inside
   the resolved root. `worktree_file_path` maps the rest to a 400 (escapes the
   root) or a 404 (missing, a directory, not a regular file), and the
   untracked entries of the list and the diff are never opened when the gate
   refuses them. The `path` itself is validated by the same `SAFE_PATH_RE` as
   the commit routes.
5. **A path is never a revision.** The committed file read uses `ls-tree --
   <path>` and `cat-file blob --end-of-options <oid>`, not `show <sha>:<path>`.
   The spy test asserts that the user's path appears in git arguments only
   whole and only after `--`, and that `cat-file` carries the boundary before
   its object id.

## Routes

Pages (`/commits`, rendered by `commits.html`, routed client-side) and their
API (`/api/commits`). The compare and refs routes are registered before
`/{commit_hash}` so their first segment is matched as a literal, like
`/repos` before them.

| Page | API | Notes |
|------|-----|-------|
| `/commits/compare?repo=&base=&head=[&mergebase=1]` | `GET /api/commits/compare` | Detail: kind, refs, resolved shas and their short forms, `merge_base`, `diff_base`, `commits` (up to 200), `commit_count`, `oldest`, `files` |
| `/commits/compare?repo=&worktree=1[&base=]` | same, `worktree=1` | `head_resolved` is null |
| | `GET /api/commits/compare/diff` | `{"files": [FileDiff]}` |
| `/commits/compare/file/<path>?...` | `GET /api/commits/compare/file/<path>` | `{content, lines}` with gutters |
| | `GET /api/commits/refs?repo=` | `{current, local, remote, default_base}` |

Errors: a bad or unknown ref is a 400 with `Invalid ref` or `Unknown ref`, a
missing file is a 404, a path escaping the root is a 400. `mergebase` and
`worktree` are flags (`1`, `true`, `yes`).

`refs`: branches come from `for-each-ref --sort=-committerdate`, remote
`*/HEAD` entries are dropped, `current` is null on a detached HEAD.
`guess_default_base` proposes the target of `refs/remotes/origin/HEAD` (as the
local branch of that name when it exists), else `main`, else `master`, else
the first other local branch, and never the current branch.

## The page

`commits.js` keeps one IIFE and three views. What changed for comparisons:

- A **target** replaces the bare hash: `{kind: 'commit', hash}` or
  `{kind: 'compare', base, head, mergebase, worktree}`. `pageUrl(view,
  target, path)` and `apiUrl(what, target, path)` build every URL from it,
  `targetFromSearch` reads it back from the query on load. Comparison state
  lives in the URL only (decision 16 of the epic).
- **Select mode** is a pure reducer in `compare-model.js` (`pick`, `range`,
  `rangeTarget`): two endpoints, order of taps irrelevant, a third tap starts
  over, a range of one is `commit^..commit`. The bar under the list opens the
  range.
- The **Compare sheet** reuses the folder picker's frame classes
  (`.picker-modal`, full height on the phone, centered on desktop) with a
  Working tree shortcut, head and base inputs, the merge-base checkbox and a
  branch list that follows the focused field.
- The **Working tree row** is loaded with the list from
  `/api/commits/compare?worktree=1` and hidden when there are no files.
- The comparison **header** is `renderCompareHeader`: the title from
  `CompareModel.title` (also the default review title, its range start taken
  from `oldest`), the kind, each ref with its own resolved short hash, the
  merge base with its hash when that mode is on (the base's hash is the
  branch tip, the merge base is where the diff starts), the commit count,
  and the included commits collapsed under it.

## The sticky file header

`.diff-file-header` is `position: sticky; top: 0` and sticks to the window,
which is the scrolling element (`.main` has no overflow). For that to work no
ancestor between the header and the window may set an `overflow` other than
`visible`, so `.diff-file-section` no longer has `overflow: hidden` for its
rounded corners: the corners are drawn on the header and on
`.diff-table-scroll`, which keeps the horizontal scroll of the table below the
header. The e2e check scrolls a long diff and asserts the header's bounding
top stays at 0 while its section's top is negative, and walks the ancestors
for any non-visible overflow.

## Later milestones

Saved reviews (the `~/.merlin/reviews/` store, the viewed-file hash rule and
the poll), comments (the re-anchoring rule) and the `merlin review` CLI are
documented here as they land.
