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
| `range` | `oldest^` | `newest` | Select mode, two taps in the list, or the Compare sheet with a commit hash as head (then the base is the merge base) |
| `branch` | `merge-base(base, head)` | `head` | The Compare sheet with a branch (or any symbolic ref) as head. The sheet always compares from the merge base |
| `worktree` | `HEAD` (or a typed base) | the files on disk | The pinned Working tree row or the sheet's shortcut |

`resolve_comparison()` turns the query (`base`, `head`, `mergebase`,
`worktree`) into a `Comparison` record: the refs as typed, `base_resolved`
and `head_resolved` (shas), `merge_base` when asked for (recomputed on every
load, so a base branch that advances is followed), and `diff_base`, the sha
every diff runs against. `head_resolved` is null for the working tree, which
has no head commit. The kind is derived: `worktree`, then in merge-base mode
`branch` for a symbolic head and `range` for a head that is a 4 to 40 hex
hash (a hash cannot move), and outside merge-base mode `commit` when
`diff_base` is the head's first parent (or the empty tree under a root
commit), `range` otherwise.

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
| | `GET /api/commits/refs?repo=` | `{current, local, remote, default_base, commits}`, the commits being the last 30 from `HEAD` for the sheet pickers |

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
  Working tree shortcut, Head and Base as two select buttons
  (`.compare-select`, `sheetValues` holds the picks) over one picker: a
  filter input and a list of the branches then the recent commits
  (`/api/commits/refs` carries `commits`, the last 30 from `HEAD`), filtered
  by `CompareModel.filterRefs`. Typed text that names nothing listed is
  offered "as typed" (`CompareModel.refIsListed`), so any ref still works.
  Choosing fills the active field and moves to the other one when it is
  empty. The sheet always submits `mergebase: true`: the checkbox went away
  on 2026-09-18 (the merge base equals the base whenever the base is an
  ancestor of the head, so the option only mattered when the base had moved
  on, where the merge base is what a review wants). The shortcut is
  disabled with `no uncommitted changes` when `worktreeState` (shared with
  the pinned row) says the tree is clean.
- The **Working tree row** is loaded with the list from
  `/api/commits/compare?worktree=1` and hidden when there are no files.
- The comparison **header** is `renderCompareHeader`: the title from
  `CompareModel.title` (also the default review title, its range start taken
  from `oldest`), the kind, each ref with its own resolved short hash, `from
  merge base <hash>` only when the merge base differs from the base's tip
  (`CompareModel.mergeBaseNote`), the commit count, and the included
  commits collapsed under it. An empty comparison shows an empty state,
  worded for the working tree when that is what it is.
- **Kinds.** `resolve_comparison` derives the kind: `worktree`, then in
  merge-base mode `branch` for a symbolic head and `range` for a head that
  is a 4 to 40 hex hash (even a range of one commit: a hash cannot move, so
  there is nothing to follow), and outside merge-base mode `commit` when the
  diff base is the head's parent, `range` otherwise (Select mode's ranges).
  A review saved before this rule with a hash head and kind `branch` takes
  the live kind on its next load (`_reconcile_kind`, under the lock). Its
  title is recomputed only when it is still the old default `<head> vs
  <base>`.

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

## Reviews: the store

A review is a saved comparison, one JSON file under `paths.reviews_dir()`
(`~/.merlin/reviews/<id>.json`, the id is `secrets.token_hex(4)`). The
module is `commits/reviews.py` and it is the only writer, for the server and
for the `merlin review` CLI alike (milestone 3), because they run in
different processes:

- **Locking.** Every read-modify-write runs inside `locked(id)`, an
  `fcntl.flock` on `<id>.lock`, through `update(id, fn)`. A plain `load()`
  needs no lock: the write is a temp file (`<id>.json.<pid>.tmp`, fsynced)
  then one `os.replace`, so a reader sees the previous or the next whole
  file. `tests/unit/test_reviews.py` has two spawned processes doing 25
  updates each on one review and asserts all 50 changes survive.
- **Malformed files.** A file that is not JSON, not an object, or carries
  another id raises `ReviewCorrupt`: the route answers 500 with the path,
  the list shows an error entry, and nothing ever overwrites it.
- **The record** is decision 7's shape: `id`, `repo` (the resolved root),
  `title`, `kind`, `base`, `head` and `mergebase` as typed (a branch review
  follows its branch, the merge base is recomputed on every load),
  `base_resolved` and `head_resolved` at creation, `last_seen_head` and
  `last_seen_at` (the user's last visit), `status` (`open` or `closed`),
  `created`, `updated`, `files` and `comments`. `updated` is stamped on
  every write and is what the poll compares.

### Viewed files

`set_viewed(id, path, True)` stores `{viewed_at, viewed_hash}` where
`viewed_hash` is the SHA-1 of `patch_text(cmp, path)`: the file's own patch
in the comparison as it is now (`git diff --end-of-options diff_base head --
path`, the working-tree equivalent, or the `--no-index` diff of an untracked
file). Not the whole diff, so an unrelated file changing never clears a
tick. `refresh(id)` (the full load) recomputes the hash of every viewed file
under the lock, drops the entries whose patch changed and returns them as
`changed_since_viewed`, and the page shows those files unticked with a
`changed` marker for that load.

### The moving head and the last visit

`refresh` also compares the review's `last_seen_head` with the head as it
resolves now. When they differ it counts `last_seen..head` (`rev-list
--count`, or everything up to the head when the old sha is gone), returns
the count as `new_commits`, and only then advances `last_seen_head`. In the
same pass it hands back `last_seen_at` as `seen_before` (a record from
before the field existed counts from its `created`) and stamps the visit.
Both advance only when `advance_seen` is on: the review page's full load
is a visit, the full-file view's load (`?visit=0`, it has no activity
panel) and the agent's `merlin review show` are not, and the poll never
calls `refresh`. A review whose repository is gone or whose branch was
deleted still shows its record and threads, so that load records the
visit too (`record_visit`, under the lock). The record is written only
when something changed, and a visit is a change.

The "since your last visit" panel is computed on the page, not stored:
`CompareModel.activitySince(review, seen_before, new_commits)` lists the
new commits and every comment, reply and resolution by an author other
than `user` whose stamp is after `seen_before`, newest first. Because the
stamp is the previous visit's, activity the agent adds while the page is
open (it arrives by the poll and re-renders the chrome) qualifies too, and
the panel empties on the visit after the one that showed it. Thread ids in
that list get the `unread` dot. Stamps are ISO 8601 in UTC with a fixed
offset, so string order is time order.

### Routes

All registered before `/{commit_hash}`.

| Route | Does |
|-------|------|
| `GET /api/commits/reviews?repo=` | Summaries of the repository's reviews, newest update first, closed ones included |
| `POST /api/commits/reviews` `{repo, base, head, mergebase, worktree, title?}` | Resolves the comparison (400 on a bad ref) and creates the review. 201 with the record |
| `GET /api/commits/reviews/<id>[?visit=0]` | The full load: `{review, comparison, changed_since_viewed, new_commits, seen_before, anchors, open_threads, error}`. A repository that is gone or a branch that was deleted gives `comparison: null` and an `error` string, the record intact. `visit=0` loads without recording the visit (the full-file view) |
| `GET /api/commits/reviews/<id>?since=<updated>` | The poll: `{"changed": false}` when the stamp matches, else `{"changed": true, "review"}`. Never recomputes or writes |
| `GET /api/commits/reviews/<id>/diff`, `.../file/<path>` | The diff and the full files of the review's comparison, resolved in the review's stored repository. Keyed by the id alone: no query parameter can point them at another repository |
| `PATCH /api/commits/reviews/<id>` `{title?, status?}` | Rename (whitespace collapsed, 200 chars, never empty) or open and close |
| `PUT /api/commits/reviews/<id>/viewed/<path>` `{viewed}` | Tick or untick a file |

Pages: `/commits/reviews`, `/commits/reviews/<id>`,
`/commits/reviews/<id>/file/<path>`.

### The page

A third target kind, `{kind: 'review', id}`. A review URL establishes its
own repository: `resolveDefaultRepo` reads the record first and selects
`review.repo`, ahead of any `?repo=`, saved repository or terminal directory,
so a bare `/commits/reviews/<id>` link works and a conflicting `?repo=` is
overridden. `loadReview` fetches the full load, keeps the record in
`currentReview`, renders the chrome (title input, status pill, refs from the
live comparison, the "Since your last visit" panel, the error line, Copy for agent, Close
or Reopen), then fetches the diff from `/api/commits/reviews/<id>/diff`, a
route keyed by the id that resolves in the stored repository (`_review_repo`
accepts only the exact stored root, never an enclosing repository), and
renders the same sections as any comparison. The full-file view of a review
uses `/api/commits/reviews/<id>/file/<path>` the same way. `applyViewedState` projects the record's
`files` onto the panel and the sections: ticked rows, collapsed sections
(`.viewed`, expanded again with `.expanded` on a header tap), the `changed`
markers, the `k of n viewed` label. `ensureReview` is the implicit creation:
a tick or Save as review on a comparison or a commit page POSTs the review,
swaps the URL with `history.replaceState` and re-renders in place. The
creation is single-flight (`CompareModel.singleFlight`): two quick ticks
share one POST and both wait for the same id, and the Save button is
disabled while it is pending. Every mutation of the record (a tick, a
rename, a status change) runs through one serial queue
(`CompareModel.serialQueue`), so two responses never overwrite each other
out of order.

The poll is one `setInterval` of 5 seconds, skipped while the document is
hidden or the view is not a review, stopped on navigation, and run once more
when the tab becomes visible. A newer record re-renders the chrome and the
viewed state, never the diff.

## Comments

A comment is a thread on a line or on the review (decision 11), stored in
the record's `comments` list:

```json
{ "id": "<8 hex>", "path": "<path or null>", "side": "new | old",
  "line": 42, "line_text": "<the line as it was>",
  "anchor_head": "<sha or null>", "author": "user | agent", "body": "…",
  "created": "<iso>", "status": "open | resolved",
  "resolved_at": "<iso or null>", "resolved_by": "user | agent | null",
  "replies": [ { "id": "<8 hex>", "author": "user | agent", "body": "…",
                 "created": "<iso>" } ] }
```

`add_comment` reads `line_text` from the current comparison through
`compare.side_lines(cmp, path, side)`: the head version (the disk for the
working tree) for `new`, the base version for `old`, both through the same
blob and containment readers as the full-file view. A line beyond the side's
length, a side the file does not have (a file added on the branch has no old
side), a missing path with a line, or a body that is empty or longer than
4000 characters is a `ValueError` (400, exit 1). Bodies are plain text with
line breaks, never interpreted. `reply`, `resolve` (with an optional closing
reply) and `reopen` are locked updates. Nothing deletes a comment or a reply.

### Re-anchoring

`anchor_comment(comment, lines, head)` is the pure rule of decision 12 over
one comment and the current lines of its side: `current` when `line_text`
is still at `line`, `moved` (with the new line) when the text occurs exactly
once elsewhere, `outdated` otherwise (gone, several occurrences, or the side
no longer exists). The comparison is exact on both sides, nothing trimmed.
`anchor_comments` applies it to every line comment whose `anchor_head`
differs from the current head (always for the working tree, whose head is
null), updates `line` and `anchor_head` of a moved comment in place, and
returns the state per id. Nothing is ever dropped. `refresh` runs it under
the lock and writes the record only when a comment moved, and the full load
returns the states as `anchors` and the open counts per path as
`open_threads`.

### Routes

| Route | Does |
|-------|------|
| `POST /api/commits/reviews/<id>/comments` `{body, path?, side?, line?}` | A new thread from the page. The author is always `user`. 201 with `{review, comment}` |
| `POST .../comments/<cid>/replies` `{body}` | A reply, from the user |
| `POST .../comments/<cid>/resolve` `{message?}` | Resolve, with an optional closing reply |
| `POST .../comments/<cid>/reopen` | Reopen |

### The page

Every diff row carries `data-side` and `data-line` (a deleted line is
`old`, added and context lines are `new`), and every number cell is
`commentable`: a tap arms the row and shows the `+` affordance (44 px on the
phone, on hover on desktop), and the affordance opens the composer row under
the line. `submitComment` goes through `ensureReview` first, so the first
comment on a plain comparison or a commit page creates the review (decision
8), then POSTs through the serial mutation queue. `renderThread` is the one
thread component (author badge, time, body, replies, Reply, Resolve or
Reopen, folded when resolved), and `renderAllThreads` places every thread of
the record on each render: under its row in the diff table or the file
table, at the top of its file section when it is outdated or its line is
not among the hunks shown (with the original line quoted), and in the
review-wide panel. The file header's count is the file's open threads. The
poll re-renders the threads with the chrome.

The full-file view shows the head version, so only `new`-side threads sit
under their line there. Old-side and outdated threads of that file render
at the top of the file view with their quote.

## The `merlin review` CLI

`commits/review_cli.py`, registered in `cli.py`'s `DELEGATED_COMMANDS` and
`ext_commands.CORE_COMMANDS` (the drift tests enforce both). It uses
`reviews.py` directly, no HTTP, so every write goes through the same lock and
atomic rename as the server, and the page picks it up within a poll. The
repository of a review is the one its record stores (checked to be exactly
a root). `--repo` only chooses which repository `list` looks at, defaulting
to the git root of the current directory. Output is markdown for an agent's
context, `--json` the raw record with its live comparison.

| Command | Does |
|---------|------|
| `list [--all] [--repo] [--json]` | The open reviews (`--all` includes closed): id, title, kind, status, open threads, updated |
| `show <id> [--all] [--json]` | Title, repo, kind, refs and hashes, the new-commits note, the files with viewed marks and open counts, every open thread with the quoted line and its replies, `[moved]` and `[outdated]` labels, resolved threads as a count unless `--all` |
| `diff <id> [--path <p>]` | The unified diff as git prints it (`compare_patch`, untracked files appended for the working tree) |
| `comment <id> [--path <p> --line <n> [--side new\|old]] [--author agent\|user] <body>` | A new thread. `line_text` read from the comparison, a missing line is an error. `-` reads the body from stdin |
| `reply <id> <cid> [--author] <body>` | A reply |
| `resolve <id> <cid> [-m <reply>] [--author]`, `reopen <id> <cid>` | Thread status |
| `close <id>`, `reopen-review <id>` | Review status |

`show` loads the review with `refresh(..., advance_seen=False)`: it
re-anchors and recomputes viewed like the page, but the agent's look does
not advance `last_seen_head` or `last_seen_at`, so the user's "since your
last visit" panel survives it. Its refs line names the merge base only when
it differs from the base, like the page header.

The agent learns the loop from the "Code reviews" section of
`agent/MERLIN.md` and the core `skills/review/SKILL.md` operating card, which
triggers on review comments, a review id or a pasted `merlin review show`
line.
