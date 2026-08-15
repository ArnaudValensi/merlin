# Agent Activity Timeline

**Status: implemented.** Timeline records private lifecycle metadata from
supported interactive agents and explicit automation, then renders it at
`/timeline`. Codex and Claude Code hook normalization, the generic emitter,
bounded storage and queries, live polling, and the Clover reference integration
are implemented. Timeline deliberately does not observe `AgentEngine`.

## Ownership

Timeline is the enabled-by-default built-in extension under `timeline/`. It uses
the same extension contract as Notes and installed extensions:

- `page_router` at `/timeline`;
- `api_router` at `/api/timeline`;
- `STATIC_DIR` at `/static/timeline`;
- `NAV_ITEMS` for the sidebar;
- `start()` for consent-aware hook reconciliation;
- `disable()` for immediate capture shutdown and hook removal;
- `commands/emit.py` and `commands/capture.py` for `merlin timeline emit` and
  `merlin timeline capture`.

`main.py` only registers the built-in. It contains no Timeline storage, capture,
or rendering policy. Authenticated mounting, user-data paths, and stable tmux
identity are generic Merlin seams. The extension owns everything else.

Disabling Timeline on `/extensions` immediately sets historical capture to
`off` and removes its marked provider hook groups through the extension's
`disable()` hook. Its page, API, navigation, and startup reconciliation are
removed after restart. Existing provider processes also re-check consent before
every write. The extension-owned `merlin timeline capture auto|ask|off` command
remains available while the web extension is disabled, so capture can be
inspected or recovered without a control page.

## Data boundary

Records live in daily append-only partitions:

```text
~/.merlin/logs/activity/YYYY-MM-DD.jsonl
```

`MERLIN_HOME` changes the `~/.merlin` root in tests or custom installations. The
directory is `0700`; partitions are `0600`. A partition accepts at most 64 MiB,
one record accepts at most 8 KiB, and partitions older than 90 days are removed
during writes. The writer checks retention once per UTC day, including while the
dashboard is stopped. Today's active file is never rewritten by retention.

The writer is dependency-light and does not contact the web server. It uses an
exclusive file lock, one append write, and UUID deduplication. A private durable
event-id index avoids rescanning the daily partition for every hook. The index
repairs an incomplete tail from the source JSONL before checking a new id.
Several hooks and harnesses can write concurrently. Default callers fail open: a
missing directory, full disk, malformed event, or lock/write failure cannot
change an agent or harness result. Strict CLI mode exists for diagnostics.
Activity and provider-file locks use a 250 ms acquisition budget, and the
provider-hook process has a two-second hard deadline. Startup reconciliation
runs off the async server loop. A stalled writer therefore loses observability
rather than stalling an agent or the dashboard indefinitely.

Malformed complete lines and truncated tails are skipped during reads and added
to the API `skipped` count. A truncated final line is left behind the cursor so a
later completed append can be read. Unknown event kinds and safe extra fields
remain readable; extra keys and values pass the same content-boundary validation
as known fields.

A bounded private capture-health sidecar counts current-day writes lost to the
daily cap, lock timeout, normalization rejection, or an I/O failure. The API
reports those drops separately from malformed records and semantic span flags.
The sidecar contains only a safe error class and timestamp, never exception text.

The query path maintains `~/.merlin/logs/activity/.span-context.json`. This
private derived index holds at most 10,000 unmatched starts and 10,000 recently
closed spans, plus an inode and byte offset for each retained partition. It lets
a bounded range query recover a span whose start is older than the range. The
index is rebuilt from the source JSONL after corruption or partition replacement;
it is not a second source of truth. Its lock and file are `0600`.

To remove retained history, first set capture to **Keep off**, then delete only
`~/.merlin/logs/activity/`. Turning capture off removes Merlin's provider hook
groups but deliberately does not erase existing records.

## Version 1 event contract

Every line is one JSON object with these fields:

| Field | Contract |
|---|---|
| `schema_version` | `1` |
| `event_id` | UUID used for idempotency |
| `timestamp` | Offset-aware timestamp normalized to UTC |
| `phase` | `point`, `start`, or `finish` |
| `kind` | Lowercase dotted name such as `agent.turn` |
| `trace_id` | Causal flow identifier |
| `span_id` | Required for start/finish, absent for a point |
| `parent_span_id` | Optional parent span |
| `actor` | `human`, `agent`, or `automation`; stable id, label, optional role |
| `context` | Provider, model, effort, project/cwd, tmux metadata, `agent_sid`, optional local artifact paths |
| `status` | `running`, `ok`, `error`, `blocked`, `timeout`, `interrupted`, or `unknown` |
| `name` | Short safe display label |
| `attributes` | Small non-content source metadata |

Point events cannot carry a span id. Starts accept `running` or `blocked`;
finishes require a terminal status. Attribute keys named like `prompt`, `input`,
`command`, `output`, `result`, `message`, `error`, `secret`, or their tool/stdout
variants are rejected. Context and attributes are each capped at 2 KiB, nesting
and item counts are bounded, and names are capped at 160 UTF-8 bytes.

The schema is provider-neutral. Implemented kinds include human prompts and
answers, agent turns and waits, tool calls, session lifecycle, review request /
await / completion / guard, automation scripts, and chain handoffs. A future
dotted kind gets neutral presentation rather than a read failure.

## Consent and provider hooks

Historical capture has its own `MERLIN_ACTIVITY_HOOKS` setting. It does not share
the ephemeral agent-state-pill consent.

- `ask` is the default. The page explains the data boundary and asks.
- `auto` installs and reconciles Timeline's marked hook groups.
- `off` removes only Timeline's marked groups and rejects new hook writes.

The setting is stored in `~/.merlin/config.env` with private permissions. A
saved choice takes precedence over a stale inherited environment value; the
panel shows whether the effective value came from config, the environment, or
the default. The capture panel on `/timeline` and `merlin timeline capture` are
the supported controls. Existing foreign Claude Code and Codex groups,
including Merlin's state-pill groups, are preserved. Null event entries written
by a provider are treated as empty lists. Malformed shapes are left untouched.
Failure to update one provider does not block the other or Merlin startup.

Codex reads `$CODEX_HOME/hooks.json`, normally `~/.codex/hooks.json`. Claude Code
reads `$CLAUDE_CONFIG_DIR/settings.json`, normally
`~/.claude/settings.json`. Codex may require the user to trust a changed hook
definition in `/hooks`. Already-open agents may need a restart to load a newly
installed group, but a loaded Timeline hook still honors a later `off` setting.
Reconciliation serializes Timeline and core state-pill writers with one shared
sidecar lock and verifies that the provider file is unchanged before replacement.
An external edit that races the update is preserved and retried later.

Hooks normalize SessionStart, prompt submission, turn completion, tool
start/end/failure, permission waits, blocking questions, and answer transitions
when the provider exposes them. Codex compaction SessionStart is ignored.
Payload text is never copied. Tool names are reduced to safe labels; prompt,
answer, command, input, result, response, model output, and secrets are not
stored. Provider hooks accept an inherited Timeline trace only when it satisfies
the identifier contract; an unsafe value falls back to the provider session
scope instead of dropping capture.

Capture requires a tmux pane. Timeline reads the core-owned `@agent_sid` and
pinned `@agent_cwd` when they exist, but never creates or changes either option
and never mutates `@agent_state`. If the core identity is not present yet, the
extension derives a private actor key from provider session and tmux metadata;
live status then follows the current tmux session/window instead. This fallback
is weaker than the stable identity but keeps Timeline capture functional when
the separately consented state pills are off. Outside tmux, with a dead/moved
pane, malformed input, or a slow/unavailable tmux server, the hook quietly
records nothing and exits successfully.

## Generic emitter

Scripts use the extension command rather than importing its schema:

```bash
merlin timeline emit --kind review.request --point \
  --trace chain-7 --name "Review requested"

merlin timeline emit --kind review.await --start \
  --trace chain-7 --span wait-7 --name "Await reviewer"

merlin timeline emit --kind review.await --finish \
  --trace chain-7 --span wait-7 --status ok --name "Reviewer signaled"
```

The command works while the dashboard is stopped. Flags override environment
metadata. The main inherited variables are `MERLIN_TIMELINE_TRACE_ID`,
`MERLIN_TIMELINE_SPAN_ID`, `MERLIN_TIMELINE_PARENT_SPAN_ID`,
`MERLIN_TIMELINE_ACTOR`, `MERLIN_TIMELINE_ACTOR_ID`,
`MERLIN_TIMELINE_ACTOR_LABEL`, `MERLIN_TIMELINE_ROLE`, `MERLIN_AGENT_SID`,
`MERLIN_PROVIDER`, `MERLIN_MODEL`, `MERLIN_EFFORT`, `MERLIN_PROJECT`,
`MERLIN_CWD`, and `TMUX_PANE`.
`MERLIN_TIMELINE_SPAN_ID` applies only to `--start` and `--finish`; a point
ignores the inherited value. Passing `--span` explicitly with `--point` remains
invalid.

Default mode returns zero when capture is not `auto` or when emission fails.
`--strict` returns non-zero for disabled capture, invalid metadata, or storage
failure. `--json` prints a result containing truthful success state, the event
id, path, duplicate state, or safe error. Use concise labels; do not pass claims,
responses, commands, or other content as names or attributes.

## Query API

`GET /api/timeline` is authenticated. With no dates it returns the most recent
hour. `since` and `until` require offset-aware ISO-8601 timestamps and may span at
most seven days. `limit` defaults to 2,000 and is capped at 10,000. Filters are
`actor`, `kind`, `status`, `project`, `provider`, and `trace`; `grouping` accepts
`participants` or `activity`.

Responses contain range metadata, stable participant/activity tracks, display
items, late finish `updates`, an opaque per-partition cursor, last-modified time,
partial state, and data-quality counts. `skipped` is unreadable storage input,
`flagged` is readable semantic inconsistency, and `dropped` is a capture failure
reported by the writer. The legacy `anomalies` value is their aggregate. Supply
the returned `cursor` to read only later bytes. Do not decode or construct
cursors outside the extension.

Span assembly is deterministic. A finish pairs with its trace/span start. Late
finishes update an already-rendered span. Duplicate starts/finishes,
finish-without-start, and clock skew stay visible as anomalies. An unfinished
agent span is open only while the board reports its stable agent id alive; a
known-dead actor is interrupted, and unavailable liveness is unknown.
Full queries consult the derived span-context index, so live and crossing spans
remain present when their starts predate `since`. Those items are marked as
continuing from before the range and their bars are clipped at both range edges.
For a historical range, a known finish after `until` is included for assembly so
the span keeps its terminal status rather than being judged by current liveness.

`MERLIN_TIMELINE_FIXTURES=1` enables deterministic API scenarios for tests and
screenshots. A query parameter cannot enable the fixture source in production.

## Page behavior

The DOM/CSS renderer keeps every item keyboard-focusable and uses deterministic
sub-lanes for overlap. Participants grouping shows Human, first-seen stable agent
tracks, then Automation. Activity grouping reuses the same data as Human input,
Agent work, Tools and scripts, Waiting, and Review and handoff.

Live mode polls the cursor about every 1.5 seconds, preserves explanatory empty
and capture-off states across those polls, grows open spans locally, and evicts
points and completed spans after they leave the sliding time window. While any
span remains open, the page periodically issues a full range query so agent
liveness is re-evaluated even when no new record advances the cursor. A dead
actor becomes interrupted and stops growing. If no finish timestamp was
observed, the duration is shown as unknown rather than zero.
One client-known open item survives a context-free full response while storage
or liveness is temporarily unavailable. A second complete response must confirm
it; a known-dead update removes it immediately. Accessible names and the detail
duration advance on the same local clock as the visible live bar.
Frozen mode continues receiving events into a buffer and merges them on resume.
Selection, focus, grouping, zoom, filters, and the chosen range survive updates
through local state and the URL. A disconnected poll keeps existing history,
shows reconnecting state, and catches up after recovery. The browser retains at
most 2,500 rendered items even when the API returns more.

Desktop details use a side drawer; narrow screens use a bottom sheet. Status is
not color-only. Local transcript and artifact links appear only when a producer
explicitly emits their paths; Timeline does not manufacture AgentEngine session
links.

## Clover reference integration

Clover's generic review and chain scripts feature-detect `merlin timeline emit`.
They record review start/register/reattach, request, the implementer's actual
await interval and outcome, completion/verdict metadata, guards, and successor
handoff states. The launcher closes the handoff span after a successful launch;
the successor's provider SessionStart emits a separate one-use causal point
under that span. Correlation sidecars expire after seven days, are capped at
4,096 keys, and clear stale provider lifecycle entries on session boundaries.
The protocol remains correct when Merlin is missing, disabled, or failing, and
it never emits review response or claim content.

## Failure and support notes

- The web server is not in the write path. Restarting it does not interrupt
  collection.
- A missing or disabled emitter is an observability no-op for harnesses.
- Store corruption is local: good records still render and the skipped count
  exposes unreadable data.
- Cursor reads are byte-oriented. Replacing or truncating a partition resets that
  partition and increments anomalies.
- Provider metadata is best-effort. Unknown model or effort is shown honestly.
- Codex is the live acceptance provider. Claude Code has committed sanitized
  fixtures and reconciler coverage; live Claude availability is recorded in the
  epic completion journal.

The canonical implementation tests are `tests/unit/test_timeline_*.py` and
`tests/e2e/test_timeline.py`. Clover's protocol integration is exercised by its
registered `agent_harness` acceptance group.
