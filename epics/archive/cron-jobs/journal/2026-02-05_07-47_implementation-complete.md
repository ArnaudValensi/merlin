# Cron Jobs Epic — Implementation Complete

**Date**: 2026-02-05
**Session**: Implementation of all tasks T1-T7

## Summary

Completed full implementation of the cron jobs system for Merlin. All tasks done and validated.

## Tasks Completed

### T1: Job file format and directory
- Created `merlin-bot/cron-jobs/` directory with `.gitignore`
- Created `_example.json.template` documenting the job file format
- Fields: description, schedule, prompt, channel, enabled, report_mode, max_turns, created_at

### T2: State and history tracking
- Created `cron_state.py` with helpers for:
  - `.state.json`: last run timestamp per job
  - `.history.json`: run history with rolling 100 limit
- Functions: get/set_last_run, append_history, get_history, get_all_history

### T3: Dispatcher (`cron_runner.py`)
- PEP 723 script with `croniter` dependency
- Loads all `*.json` jobs from `cron-jobs/` (skips dotfiles, templates)
- Checks schedule using croniter + last run time
- Builds prompt with report_mode instruction
- Calls `invoke_claude()` with deterministic session ID
- Updates state and history after each run
- Graceful error handling (one bad job doesn't stop others)
- Logging to `logs/cron_runner.log`

### T4: Cron skill (SKILL.md)
- Created `.claude/skills/cron/SKILL.md`
- Operations: add, edit, list, enable/disable, remove, history
- Conversational flow with confirmation before creating/modifying
- Cron expression cheat sheet for reference

### T5: Unit tests
- 40 tests across `test_cron_state.py` and `test_cron_runner.py`
- Coverage: state tracking, history tracking, job loading, schedule checking, prompt building, session ID, dispatcher behavior
- All tests passing

### T6: Crontab setup and docs
- Updated `CLAUDE.md` with cron documentation
- Includes: job format, crontab setup (with cronie install), verification commands, manual management, log locations

### T7: Integration test
- Created test job `integration-test.json`
- Ran dispatcher manually
- Verified:
  - Job loaded correctly
  - Schedule check passed (job was due)
  - Claude invoked with proper prompt and report_mode suffix
  - Discord message sent successfully
  - State updated with last run time
  - History entry added with exit_code, duration, session_id
  - Claude invocation logs written

**Test output**:
```
07:46:28 INFO     Dispatcher started at 2026-02-05T07:46:28.566014+00:00
07:46:28 INFO     Loaded 1 job(s)
07:46:28 INFO     Running job integration-test (channel=YOUR_CHANNEL_ID, max_turns=5)
07:46:30 INFO     Session 466eb690-095e-50e2-a636-a13614fa2ea9 not found, creating new session
07:46:42 INFO     Job integration-test completed successfully (12.3s)
07:46:42 INFO     Dispatcher finished
```

## Files Created/Modified

**New files**:
- `merlin-bot/cron-jobs/.gitignore`
- `merlin-bot/cron-jobs/_example.json.template`
- `merlin-bot/cron_state.py`
- `merlin-bot/cron_runner.py`
- `merlin-bot/.claude/skills/cron/SKILL.md`
- `merlin-bot/tests/test_cron_state.py`
- `merlin-bot/tests/test_cron_runner.py`

**Modified files**:
- `CLAUDE.md` (added cron documentation)
- `epics/cron-jobs/tasks.md` (status updates)

## Notes

- Cron skill (T4) allows Merlin to manage jobs via Discord. The full conversational flow should be tested live by messaging Merlin with requests like "add a cron job" or "list my cron jobs".
- The crontab entry requires cronie to be installed (`sudo pacman -S cronie`).
- Each job gets a deterministic session ID, so Claude maintains memory across runs of the same job.

## Next Steps

- Epic is complete and ready to be archived
- Optional: Live test of cron skill via Discord
- Optional: Install cronie and verify real crontab execution
