---
name: review
description: Work the code reviews the user leaves on Merlin's Commits page. Use when the user mentions review comments, a review id, or pastes a `merlin review show` line, or asks whether their remarks were addressed.
user-invocable: false
allowed-tools: Bash, Read
---

# Review Skill

The user reviews your commits, branches or working tree on the Commits page, from the
phone, and leaves comments on lines or on the whole review. This card is the loop you run
to address them. The full reference is `docs/dev/commit-review.md` in the Merlin repo.

## 1. Read the review

```bash
merlin review list                 # open reviews of the repository you are in
merlin review show <id>            # refs, files with viewed marks, every open thread
merlin review show <id> --all      # resolved threads too
merlin review diff <id> [--path p] # the comparison's diff as git prints it
```

`show` prints markdown. Each open thread is a heading `### <thread-id> · path:line (side)
· author · when`, the quoted line, the body and the replies. A `[moved]` thread followed
its line to a new number, an `[outdated]` thread lost its line: read the quoted text and
answer the intent. A review-wide thread has no line.

## 2. Address each thread

For every open thread, in order:

1. Read the quoted line in the file (`merlin review diff <id> --path <path>` when you need
   the context), make the change, commit as usual.
2. Reply with what you did, naming the commit when there is one:

   ```bash
   merlin review reply <id> <thread-id> "Renamed to fetch_user in 4a5b6c7."
   ```

3. Resolve it, with a closing line when the reply above did not say it all:

   ```bash
   merlin review resolve <id> <thread-id> -m "Done."
   ```

When you disagree or need a decision, say so in a reply and leave the thread open. Never
resolve silently, never skip a thread, never delete anything: the user sees every reply on
the phone within seconds.

## 3. Your own remarks

You can open a thread too, on a line or on the review:

```bash
merlin review comment <id> --path a.py --line 12 "This retry hides a real failure, keep it?"
merlin review comment <id> "All threads addressed, the branch is ready."
```

Close a review only when the user asked for it (`merlin review close <id>`).

## Notes

- The default author of the CLI is `agent`. The user's own comments say `user`.
- `show` does not count as the user's look at the review: the "N new commits since you last
  looked" note stays theirs.
- A line that does not exist on that side is an error: check the line numbers in the diff.
