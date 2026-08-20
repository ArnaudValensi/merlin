#!/bin/bash
# Deprecated shim. The restart logic now lives in `merlin restart`
# (cli.py -> server_control.py). This file is kept for ONE release so that an
# update triggered by an *older* Merlin — whose code runs `bash current/restart.sh`
# after flipping the symlink — still restarts onto the new version. New code
# calls `merlin restart` directly and never touches this file.
#
# REMOVE this shim in the release after the one that introduces `merlin restart`.
cd "$(dirname "$0")" || exit 1
exec uv run cli.py restart "$@"
