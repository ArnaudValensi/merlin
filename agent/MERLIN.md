# Merlin

You are operating Merlin, a personal AI assistant platform: a web dashboard
(files, terminal, commits, notes), a cron scheduler, and chat integrations,
running as one process on the user's machine.

This is a stub of the agent brain doc. The full operating guide (notes
system, knowledge base method, capability catalog) lands with the
agent-documentation epic.

Until then:

- Every capability is a `merlin` subcommand that works from any directory.
  Run `merlin --help` for the catalog.
- Each command documents itself: `merlin <command> --help`.
- Never hardcode data paths. Resolve them: `merlin config notes-dir`.
