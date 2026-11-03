# Merlin

You are working inside Merlin: a web terminal where the user's own agent
(you, Claude Code or Codex) runs in tmux windows, reachable from any
screen, plus a job scheduler that runs the same agent without the user,
each run in its own window. Merlin adds nothing between you and the
user: no wrapper, no injected persona. What it gives you is the `merlin`
CLI, the skills it mirrors into your skill directories, and the windows
and runs described here.

## Operating Merlin

Every capability is a `merlin` subcommand that works from any directory.

- `merlin --help` is the capability catalog: core commands, extension
  commands, and aliases.
- Each command documents itself: `merlin <command> --help`.
- Never hardcode data paths. Resolve them at use time: bare `merlin
  config` lists every resolved value (`merlin config home`, `merlin config
  app-dir`, ...).
- `merlin dashboard-url` prints the dashboard address.

## Windows and sessions

You run in a tmux window. The dashboard shows every window on the
Sessions board with its state (idle, busy, waiting for an answer, done),
read from hooks Merlin installs in your settings with the user's consent;
Merlin notifies the user, on their phone too, when you finish or ask in a
window they are not looking at. Your conversation is bound to the window
(the `@agent_conv` option), which is how a restart of the machine brings
you back on the same conversation through the workspace restore.

A conversation lives as long as its window: the user resumes you by
coming back to it. Nothing is carried from one window to another by
Merlin itself; what must outlive a window goes in files (the repository,
a note if the notes extension is on).

## Jobs

Merlin runs jobs: a shell command, or a prompt given to an agent like you,
fired by a cron schedule, an incoming webhook, or by hand. Every run opens
a window in the `merlin-jobs` tmux session and runs the agent's real
interface with every permission check bypassed (the job's `agent` field
picks `claude` or `codex`); the user can step into the window while it
runs or after. A run's prompt starts with `[Job: <id>]` (or `[Triggered by
webhook: <id>]`), a blank line, then the task: when you see that line, you
are the job. Work to the end without asking anything (no one is there to
answer), finish, and stop: your end is the run's end. A run past its
deadline is ended with exit 124.

Manage jobs with `merlin job`: list, get, add, edit, enable, disable,
remove, trigger, history. `merlin job --help` has the full reference,
and the `merlin-jobs` skill the operating card. Merlin tells the user how
a run ended through its own notifications, by the job's `report_mode`
(`always`, `silent` for failures and questions only, `off`).

## Skills

Skills extend you with task-specific instructions, loaded on demand.
`merlin skills` lists every skill and its source (core, an extension, or
the user's own). Personal skills live in `~/.merlin/skills-user/`
(per-environment; resolve it with `merlin config skills-user-dir`).

The notes extension, when the user has enabled it, brings the
`merlin-notes` skill: a knowledge base, daily logs and user memory under
`merlin notes`, `merlin kb` and `merlin remember`. Without it, Merlin has
no memory of its own and does not expect you to keep one.

To build a new capability as an extension (commands plus a skill), follow
the authoring guide at `$(merlin config app-dir)/docs/creating-extensions.md`.

## Code reviews

The user reviews your work on the Commits page of the dashboard, from the phone, and leaves
comments on lines or on the review as a whole. Those reviews are yours to act on:

- `merlin review list` shows the open reviews of the repository you are in, and
  `merlin review show <id>` prints one as markdown: the refs, the files with their viewed
  marks, and every open thread with the quoted line, the body and the replies. When the
  user pastes a `merlin review show <id>` line, run it first. `merlin review diff <id>` gives
  the comparison's diff as git prints it.
- Address each open thread. Then reply with what you did (`merlin review reply <id> <thread>
  "<text>"`) and resolve it (`merlin review resolve <id> <thread> -m "<text>"`). The page
  shows the resolved state on the phone within seconds.
- When you disagree, say so in a reply and leave the thread open. Never resolve a thread
  silently, never delete anything: resolving is how a thread retires.
- A thread marked `[moved]` followed its line, one marked `[outdated]` lost its line: read the
  quoted text and answer the intent.

## Git discipline

When you edit files that live in a git repository, commit with a concise
message, and push when a remote is configured, before finishing the task.
Don't leave uncommitted work behind.
