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
  and a **Base** select. Tap one and pick from the list under it: the
  repository's branches (local, then remote, the current one marked), then
  the 30 most recent commits, newest first, with their hash, subject and
  age. The filter box narrows both groups as you type, and any ref git
  understands can be typed there and taken **as typed** (`HEAD~3`, a tag,
  `origin/main`). Head defaults to the current branch and base to the
  repository's main branch (the target of `origin/HEAD`, else `main`, else
  `master`). The comparison always shows what the head added since it
  forked from the base, even if the base moved on since (GitHub's
  three-dot view): there is nothing to tick, and the header says `from
  merge base <hash>` only when that fork point differs from the base's
  tip. Picking a specific commit as head gives a range, even of one
  commit: a commit does not move, so there is nothing to follow.
- **Working tree**: when the tree is dirty, a pinned row at the top of the
  list reads `Working tree · N files · +x -y` and opens the uncommitted
  changes (staged and unstaged together, plus untracked files shown as
  added) against `HEAD`. The same shortcut sits at the top of the Compare
  sheet, greyed out with `no uncommitted changes` when the tree is clean.
  This is usually what the agent just did.

The comparison page header names what is compared: the kind (Commit,
Range, Branch, Working tree), both refs with their resolved short hashes,
the merge base when it is not the base itself, and the number of commits
included. A collapsible `N commits` panel under it lists those commits
(up to 200), each tappable to open on its own. A comparison with nothing
in it says so (`The working tree is clean` for the tree).

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
- **Since your last visit.** Coming back to a review, a panel under its
  header lists what happened while you were away, newest first: commits
  that landed on the head (a branch or range review follows its branch),
  and the agent's comments, replies and resolutions, each with the file
  and line and the first words. Tap a row to jump to its thread. Threads
  with such activity carry a blue dot until your next visit. Your own
  actions are not listed: you know what you did. The panel stays live
  while the page is open, so a reply the agent makes now appears there
  too, and it empties on the visit after the one that showed it.
- **Open and closed.** A review is open or closed, nothing more. The list
  page shows the repository's open reviews above the commits, with a
  collapsed **Closed** group. A closed review stays readable and can be
  reopened.
- **Copy for agent** copies `merlin review show <id>` so you can paste it
  in the terminal. The page refreshes its review chrome every 5 seconds
  while it is visible, so what the agent does from the CLI shows up on the
  phone without a reload.

## Comments

Comments are how a review reaches your agent. They live in the review's
file, never anywhere else, and they come in two shapes: on a line, or on
the review as a whole.

- **On a line.** In a diff or in the full file, tap a line number: a small
  **+** appears on that row. Tap it to open a composer under the line and
  write the comment (plain text, line breaks kept, no markdown). A deleted
  line carries its comment on the old side, added and context lines on the
  new side. The first comment on a plain comparison saves it as a review,
  like the first viewed tick.
- **On the review.** The **Comments on the review** panel above the files
  has an **Add a comment** button for remarks that belong to no line.
- **Threads.** A comment is a thread: it shows who wrote it (`you` or
  `agent`), when, its body, the replies indented under it, a **Reply**
  field and **Resolve**. A resolved thread folds to one line (`Resolved · 2
  replies`) with a **Show** button and a **Reopen** button inside. Nothing
  is ever deleted: resolving is how a thread retires. The file header
  counts that file's open threads.
- **When the code moves.** A thread remembers the text of its line. When
  the branch moves on, a thread whose line is still there stays put, one
  whose line moved (the text now sits at one other place) follows it with a
  small **moved** tag, and one whose line is gone shows at the top of its
  file as **outdated**, with the original line quoted, still answerable and
  resolvable.
- **Live.** The page refreshes its threads every 5 seconds while visible,
  so a reply or a resolve made by the agent from the terminal appears on
  the phone without a reload.

## Working with your agent

The loop this page is built for: you annotate on the phone, the agent works
the list, the page shows the resolved state.

1. Open a review, leave your comments, then tap **Copy for agent** (or type
   the command it copies) and paste it in the terminal:

   ```
   merlin review show 3f9a1c2d
   ```

2. The agent reads the review as markdown: the refs, the files with their
   viewed marks, every open thread with the quoted line and the replies.
   `merlin review diff 3f9a1c2d` gives it the diff as git prints it, and
   `merlin review list` the repository's open reviews.

3. The agent addresses each thread, replies with what it did and resolves
   it:

   ```
   merlin review reply 3f9a1c2d 8b1c2d3e "Renamed to fetch_user in 4a5b6c7."
   merlin review resolve 3f9a1c2d 8b1c2d3e -m "Done."
   ```

   When it disagrees it says so in a reply and leaves the thread open for
   you. The agent can also comment on a line (`merlin review comment <id>
   --path a.py --line 12 "..."`) and close the review when everything is
   settled.

The agent's `show` does not count as a visit: the **Since your last visit**
panel is yours. Your Merlin agent learns all of this from its
brain doc and the `review` skill, so pasting the `show` line is enough.

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
  was deleted, or the repository moved. The review, its ticks and its
  comments are kept in `~/.merlin/reviews/<id>.json`, and the page shows
  the error instead of the diff (the threads still render in the panel).
- **"has N lines on the new side"** from `merlin review comment`: the line
  does not exist in the current version of that file on that side. Check
  with `merlin review diff`.
