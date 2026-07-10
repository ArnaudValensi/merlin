# Merlin — Release Process

## Creating a Release

Tag and push — that's it:

```bash
git tag v0.1.0
git push --tags
```

No GitHub Release needed. GitHub auto-serves source tarballs for any tag. No build step — the tarball is the source code, run via `uv run`.

`CHANGELOG.md` at the tag is the user-facing changelog: the settings page links to `blob/v<tag>/CHANGELOG.md`, so commit the changelog entry before tagging (the release flow already does this).

## How Installs Work

The install script (`install.sh`) and update command (`merlin update`) both:
1. Fetch the latest tag from `https://api.github.com/repos/<owner>/<repo>/tags`
2. Download the source tarball from GitHub's auto-generated URL
3. Extract to `~/.merlin/versions/<tag>/`
4. Point `~/.merlin/current` symlink to the new version

## Version Naming

- Tags follow semver: `v0.1.0`, `v0.2.0`, `v1.0.0`
- The `v` prefix is stripped for folder names: `~/.merlin/versions/0.1.0/`
- Version detection:
  - **Dev mode**: `git describe --tags` (e.g., `0.1.0-3-gabcdef`)
  - **Installed mode**: reads the `current` symlink target folder name

## Directory Layout (Installed)

```
~/.merlin/
├── bin/
│   └── merlin           # Launcher script (added to PATH)
├── versions/
│   ├── 0.1.0/           # Extracted release
│   └── 0.2.0/           # Next release (old versions kept)
├── current -> versions/0.2.0  # Symlink to active version
├── config.env           # User configuration
├── notes/               # User data (survives updates)
├── jobs/           # Scheduled jobs
├── logs/                # Logs
└── data/                # Session registry, etc.
```

## Testing Locally

### Test the install script (dry-run)
```bash
bash install.sh --dry-run
```

### Test update
```bash
# Set up a fake installed environment
mkdir -p /tmp/test-merlin/versions/0.1.0
ln -sfn /tmp/test-merlin/versions/0.1.0 /tmp/test-merlin/current
MERLIN_HOME=/tmp/test-merlin uv run cli.py update
```

### Run the full validation pipeline
```bash
uv run scripts.py validate
```

## Rollback

To rollback to a previous version, re-point the symlink:
```bash
ln -sfn ~/.merlin/versions/0.1.0 ~/.merlin/current
```

Old versions are kept in `~/.merlin/versions/` and are never auto-deleted.

## Pre-release Checklist

1. All tests pass (core + bot)
2. `uv run main.py` starts cleanly
3. `uv run cli.py version` shows expected version
4. `bash install.sh --dry-run` output looks correct
5. Tag and push
