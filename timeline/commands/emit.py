#!/usr/bin/env python3
"""Emit a private Timeline point or span boundary without a running server.

Examples:
  merlin timeline emit --kind review.request --point --trace chain-7 --name "Review requested"
  merlin timeline emit --kind review.await --start --trace chain-7 --span wait-7 --name "Await reviewer"
  merlin timeline emit --kind review.await --finish --trace chain-7 --span wait-7 --status ok --name "Reviewer signaled"
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path


EXTENSION_DIR = Path(__file__).resolve().parents[1]
APP_DIR = EXTENSION_DIR.parent
sys.path.insert(0, str(APP_DIR))

from timeline.consent import capture_mode
from timeline.protocol import ProtocolError, validate_record
from timeline.writer import WriterError, append_record


def _env(name: str, fallback: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value not in (None, "") else fallback


def _attribute(value: str) -> tuple[str, str]:
    key, separator, item = value.partition("=")
    if not separator or not key or not item:
        raise argparse.ArgumentTypeError("attributes use KEY=VALUE")
    return key, item


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="merlin timeline emit",
        description=(
            "Append one sanitized activity event. The dashboard need not be running. "
            "Capture is fail-open unless --strict is supplied."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--kind", required=True, help="Dotted event kind, e.g. review.request"
    )
    phase = parser.add_mutually_exclusive_group(required=True)
    phase.add_argument("--point", action="store_const", dest="phase", const="point")
    phase.add_argument("--start", action="store_const", dest="phase", const="start")
    phase.add_argument("--finish", action="store_const", dest="phase", const="finish")
    parser.add_argument(
        "--trace", help="Causal trace id; overrides MERLIN_TIMELINE_TRACE_ID"
    )
    parser.add_argument("--span", help="Span id for --start/--finish")
    parser.add_argument("--parent", help="Parent span id")
    parser.add_argument("--event-id", help="UUID; generated when omitted")
    parser.add_argument(
        "--timestamp", help="ISO-8601 timestamp; defaults to now in UTC"
    )
    parser.add_argument(
        "--name", required=True, help="Short safe label, never raw content"
    )
    parser.add_argument(
        "--status",
        choices=[
            "running",
            "ok",
            "error",
            "blocked",
            "timeout",
            "interrupted",
            "unknown",
        ],
        help="Defaults to running for start and ok otherwise",
    )
    parser.add_argument("--actor", choices=["human", "agent", "automation"])
    parser.add_argument("--actor-id")
    parser.add_argument("--actor-label")
    parser.add_argument("--role")
    parser.add_argument("--provider")
    parser.add_argument("--model")
    parser.add_argument("--effort")
    parser.add_argument("--project")
    parser.add_argument("--cwd")
    parser.add_argument("--tmux-session")
    parser.add_argument("--tmux-window")
    parser.add_argument("--tmux-pane")
    parser.add_argument("--agent-sid")
    parser.add_argument("--session-file")
    parser.add_argument("--artifact-path")
    parser.add_argument(
        "--attribute",
        action="append",
        type=_attribute,
        default=[],
        metavar="KEY=VALUE",
        help="Small non-content metadata; repeatable",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Return non-zero on disabled capture or errors",
    )
    parser.add_argument(
        "--json", action="store_true", help="Print a machine-readable result"
    )
    return parser


def _timestamp(value: str | None) -> str:
    if not value:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return normalized
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def event_from_args(args: argparse.Namespace) -> dict:
    trace_id = args.trace or _env("MERLIN_TIMELINE_TRACE_ID") or str(uuid.uuid4())
    span_id = (
        args.span
        if args.phase == "point"
        else args.span or _env("MERLIN_TIMELINE_SPAN_ID")
    )
    parent_id = args.parent or _env("MERLIN_TIMELINE_PARENT_SPAN_ID")
    actor_type = args.actor or _env("MERLIN_TIMELINE_ACTOR") or "automation"
    agent_sid = args.agent_sid or _env("MERLIN_AGENT_SID")
    actor_id = (
        args.actor_id
        or _env("MERLIN_TIMELINE_ACTOR_ID")
        or agent_sid
        or ("human" if actor_type == "human" else "automation")
    )
    actor_label = (
        args.actor_label
        or _env("MERLIN_TIMELINE_ACTOR_LABEL")
        or ("Human" if actor_type == "human" else "Automation")
    )
    status = args.status or ("running" if args.phase == "start" else "ok")
    context = {
        "provider": args.provider or _env("MERLIN_PROVIDER"),
        "model": args.model or _env("MERLIN_MODEL"),
        "effort": args.effort or _env("MERLIN_EFFORT"),
        "project": args.project or _env("MERLIN_PROJECT"),
        "cwd": args.cwd or _env("MERLIN_CWD", os.getcwd()),
        "tmux_session": args.tmux_session or _env("MERLIN_TMUX_SESSION"),
        "tmux_window": args.tmux_window or _env("MERLIN_TMUX_WINDOW"),
        "tmux_pane": args.tmux_pane or _env("TMUX_PANE"),
        "agent_sid": agent_sid,
        "session_file": args.session_file or _env("MERLIN_SESSION_FILE"),
        "artifact_path": args.artifact_path or _env("MERLIN_ARTIFACT_PATH"),
    }
    return validate_record(
        {
            "schema_version": 1,
            "event_id": args.event_id or str(uuid.uuid4()),
            "timestamp": _timestamp(args.timestamp),
            "phase": args.phase,
            "kind": args.kind,
            "trace_id": trace_id,
            "span_id": span_id,
            "parent_span_id": parent_id,
            "actor": {
                "type": actor_type,
                "id": actor_id,
                "label": actor_label,
                "role": args.role or _env("MERLIN_TIMELINE_ROLE"),
            },
            "context": {
                key: value for key, value in context.items() if value is not None
            },
            "status": status,
            "name": args.name,
            "attributes": dict(args.attribute),
        }
    )


def _print_result(args: argparse.Namespace, payload: dict) -> None:
    if args.json:
        print(json.dumps(payload, separators=(",", ":")))


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    mode = capture_mode()
    if mode != "auto":
        _print_result(args, {"ok": False, "state": "disabled", "mode": mode})
        return 3 if args.strict else 0
    try:
        event = event_from_args(args)
        result = append_record(event, strict=args.strict)
    except (ProtocolError, WriterError, ValueError) as exc:
        message = str(exc)
        _print_result(args, {"ok": False, "error": message})
        if args.strict:
            print(f"timeline emit: {message}", file=sys.stderr)
            return 2
        return 0
    _print_result(
        args,
        {
            "ok": result.ok,
            "duplicate": result.duplicate,
            "event_id": event["event_id"],
            "path": str(result.path) if result.path else None,
            "error": result.error,
        },
    )
    return 0 if result.ok or not args.strict else 2


if __name__ == "__main__":
    raise SystemExit(main())
