# Commits

The Commits page (sidebar: **Commits**, route `/commits`) is a read-only
git history browser: a commit list with search and date filters, colored
unified diffs, and a full-file view with change gutters and syntax
highlighting. It shows one repository at a time and defaults to wherever
your terminal session is working, so this is where review happens:
your agent codes in the [web terminal](terminal.md), and Commits is where
you inspect what actually landed, from the same dashboard, phone included.

![Commits list on a phone](commits/phone-commits.jpg)

## Browse the history

Each row shows the short hash (blue, monospace), the one-line message,
the author, a relative time ("5m ago"), and the +insertions/-deletions
counts in green and red. Tap a commit to open its diff. Commits load 50
at a time; a **Load more** button appears at the bottom when there are
more to fetch.

## Search and filter

The search box filters commit messages as you type (case-insensitive
regex, `git log --grep` under the hood, debounced so it does not fire on
every keystroke). The **Since** and **Until** date inputs map to
`git log --since` / `--until` and reload the list when changed.

## Compare

A commit is one comparison among several. The page can compare any two
points of the repository and shows the result with the same file list,
diff sections and full-file view a commit gets. Three ways to pick what
to compare, all from the phone:

- **Select** (the toggle in the filters bar) puts the list in selection
  mode. Tap a commit to pick one end of a range, tap another to complete
  it (both inclusive, the order of taps does not matter). A bar at the
  bottom reads `N commits selected` with a **Compare** button that opens
  the range. Tapping a picked commit again clears it. There is no long
  press: it fights the browser on a phone.
- **Compare** (the button in the header) opens a sheet with a **Head**
  and a **Base** field. Each shows the repository's branches (local, then
  remote) filtered as you type, and any ref git understands works as
  typed (`HEAD~3`, a tag, `origin/main`). Head defaults to the current
  branch and base to the repository's main branch (the target of
  `origin/HEAD`, else `main`, else `master`). **Against merge base** is on
  by default: the comparison then shows only what the head branch added
  since it forked, even if the base moved on since (GitHub's three-dot
  view). Untick it for a plain two-point diff.
- **Working tree**: when the tree is dirty, a pinned row at the top of the
  list reads `Working tree · N files · +x -y` and opens the uncommitted
  changes (staged and unstaged together, plus untracked files shown as
  added) against `HEAD`. The same shortcut sits at the top of the Compare
  sheet. This is usually what the agent just did.

The comparison page header names what is compared: the kind (Commit,
Range, Branch, Working tree), both refs with their resolved short hashes,
whether the merge base is used, and the number of commits included. A
collapsible `N commits` panel under it lists those commits (up to 200),
each tappable to open on its own.

A comparison is just a URL (`/commits/compare?base=...&head=...`), so it
reloads, bookmarks and pastes like a commit link.

## Reviews

A review is a saved comparison with your progress on it. Nothing leaves
this Merlin: reviews live as plain JSON files under `~/.merlin/reviews/`,
one per review, where your agent can read them too.

- **Creating one.** On any comparison (or a single commit), the first file
  you tick as viewed saves it as a review and the URL becomes
  `/commits/reviews/<id>`, quietly, with back still returning to the list.
  **Save as review** does the same with nothing ticked yet. The title
  defaults to `head vs base` for a branch, the commit's subject for a
  commit, `oldest..newest (N commits)` for a range and `Working tree` for
  the tree. Tap the title to rename it.
- **Viewed files.** Every file, in the files panel and in its own diff
  header, has a checkbox. A viewed file collapses to its header (tap the
  header to open it again), and the panel's toggle reads `k of n viewed`.
  The tick belongs to the file's diff as it is now: when a later commit
  or an edit changes that file's diff, the tick clears on the next load and
  the file shows a small **changed** marker. Files whose diff did not
  change keep their tick, even in a branch review whose base moved on.
- **A moving head.** A branch or range review follows its branch. When
  commits landed since you last looked, the page says `N new commits since
  you last looked`, once.
- **Open and closed.** A review is open or closed, nothing more. The list
  page shows the repository's open reviews above the commits, with a
  collapsed **Closed** group. A closed review stays readable and can be
  reopened.
- **Copy for agent** copies `merlin review show <id>` so you can paste it
  in the terminal. The page refreshes its review chrome every 5 seconds
  while it is visible, so what the agent does from the CLI shows up on the
  phone without a reload.

## Read a diff

![Diff viewer](commits/phone-diff.jpg)

The diff view header shows the commit's message, short hash, author, and
relative time (or the comparison header above). A collapsible "N files
changed" panel lists each file with
a status letter (M modified in yellow, A added in green, D deleted in
red, R renamed in purple), its path, and +/- stats; tapping a file
scrolls to its diff section. Diffs render with old/new line numbers,
additions tinted green, deletions red, context dimmed. Binary files show
a "Binary file" notice instead of a diff.

Each file's header (its path and the **Full file** button) sticks to the
top of the screen while you scroll through that file's diff, and the next
file's header pushes it away. You always know which file you are reading,
on desktop and on the phone.

The back arrow returns from the diff to the list (and from a file back to
the diff). Browser back/forward works too.

## Open the full file

Each diff section has a **Full file** button (hidden for deleted files)
that opens the complete file as it existed at that commit (at the head of
a comparison, or on disk for the working tree), with syntax
highlighting picked from the file extension (auto-detect fallback).
Tapping a hunk header (the `@@` line) also jumps into the full file,
scrolled to that hunk, with diff mode already on.

In the file view:

- A thin colored gutter marks changed lines: green added, red deleted (at
  the nearest line), blue modified. Changed lines get a matching
  background tint.
- The floating prev/next arrows (bottom-right) cycle through the change
  hunks with an `n/m` counter, centering each one on screen.
- The **Diff** button in that floating cluster reveals deleted lines
  inline as red rows. Off by default.
- The **Wrap** button in the header toggles long lines between horizontal
  scroll and wrapping.

## Switch repositories

The repo indicator at the top shows the current repo path (shortened to
`~/...`). Its folder button opens a picker: browse directories with
breadcrumbs (git repos get a green branch icon) and hit **Use this
folder** (it snaps to the enclosing git root), or just type to
search every git repo under your home directory by name (found with
`fd`; plain substring match).
Escape or tapping outside closes it.

On first open, Merlin resolves a default repo in this order:

1. The `?repo=` URL parameter.
2. The last repo you used here (remembered by the browser).
3. Your active terminal pane's working directory, if it is a git repo.
4. The directory where `merlin` was launched.
5. Failing all of that, an empty state with a **Pick a project** button.

So by default you are reviewing exactly the repo your terminal session is
working in.

## Share deep links

URLs are real routes: `/commits?repo=...`, `/commits/<hash>?repo=...`,
`/commits/<hash>/file/<path>?repo=...`, and for comparisons
`/commits/compare?repo=...&base=...&head=...[&mergebase=1]`,
`/commits/compare?repo=...&worktree=1`,
`/commits/compare/file/<path>?...`, and for reviews
`/commits/reviews/<id>` and `/commits/reviews/<id>/file/<path>`. Bookmark them, or paste one
in chat to point your agent (or a friend) at a specific commit or
comparison.

## Mobile notes

- The commit list hides the +/- stats on narrow screens to save width.
- Code blocks go edge-to-edge and page padding drops to zero, so the diff
  gets the full screen width.
- Diff tables scroll horizontally with touch momentum while the file
  header stays put, and vertically the header sticks to the top of the
  screen for the length of its file.
- Selection mode uses two taps, never a long press, and its bar keeps a
  44px **Compare** button within thumb reach.
- The prev/next cluster shrinks and tucks into the corner; commit rows
  and buttons keep 44px tap targets.
- The repo picker opens as a full-height sheet on the phone (a centered
  modal on desktop).

## Troubleshooting

- **"No repository selected"**: none of the defaults resolved (no URL
  param, no saved repo, terminal CWD not a git repo, launch directory not
  a git repo). Hit **Pick a project** and choose one.
- **"Not a git repository: \<path\>"**: you pointed the page at a plain
  directory. The picker only enables the **Use** button inside a git
  root, so this mostly happens with a hand-typed `?repo=` URL.
- **A previously used repo disappeared**: if the saved repo no longer
  exists (or is no longer a repo), it is silently dropped and the default
  chain continues. Just pick it again.
- **"No commits found"**: your search or date filter matches nothing, or
  the repo has no commits yet. Clear the filters.
- **Repo search returns nothing**: fuzzy search shells out to `fd`.
  Merlin checks for it at startup and refuses to start without it, so in
  a running instance the search works; if you see an `fd` error, finish
  installing the dependencies it asked for.
- **404 opening a file**: the file does not exist at that commit (it was
  added later or lived elsewhere). Paths with unusual characters, leading
  slashes, or `..` are also rejected.
- **"Unknown ref" or "Invalid ref" on a comparison**: a typed ref did not
  resolve to a commit, or has a shape git could mistake for an option
  (a leading `-`, a `..`, a space). Pick a branch from the list or check
  the spelling.
- **"Nothing to compare"**: the two points have the same tree, for
  example a branch compared to itself or a clean working tree.
- **A review shows "Unknown ref" or "Repository not found"**: its branch
  was deleted, or the repository moved. The review and its ticks are kept
  in `~/.merlin/reviews/<id>.json`, and the page shows the error instead
  of the diff.
