"""A job run: its private directory, its run script and its tmux window.

Every job run, agent or script, lives in a window of the ``merlin-jobs``
tmux session (``terminal.tmux.JOBS_SESSION_NAME``). What the launch needs
sits in a private directory, ``~/.merlin/job-runs/<run_id>/`` (mode 0700):

- ``meta.json``: the run's facts (job id, the job, the trigger, the type and
  the agent, the working directory, the start time, the deadline, the window
  target once opened). The orphan finalizer reads it when the supervisor died
  with its run.
- ``run.sh``: what the window runs. It waits for the window options, exports
  the ``MERLIN_JOB_*`` variables and the Merlin home, runs the job in its own
  process group (``set -m``, so a deadline can kill the job and leave the
  script to stamp the end), writes the exit code atomically to ``exit`` and
  the ``@job_exit`` window option, stamps ``@agent_state done`` if the state
  is not already ``done``, and execs an interactive login shell so the window
  stays readable.
- ``prompt``: the agent's prompt, read by ``run.sh``. It is never put on a
  tmux command line: the window command is only ``bash <run.sh>``.
- ``pid``: the process group of the job and its start ticks, ``status``:
  the job's own exit code, ``output.log``: a command's output (``tee``),
  ``exit``: the run's exit code, published no-clobber (a hard link of a
  private temporary file: the first publisher wins, the other reads the
  winner), ``outcome.json``: the canonical record of the finished run,
  written first, ``result.json``: written once history and the run log are
  materialized from it (the exactly-once marker; a crash in between is
  repaired by materializing again, idempotently by run id).

The window carries ``@job_id``, ``@job_run``, ``@job_report`` and
``@job_started``, plus ``@agent_sid`` (``job-<run_id>``), ``@agent_cwd`` and
an initial ``@agent_state`` (``busy`` for a command, ``idle`` for an agent
until its hooks take over), all stamped before the job's process can start:
``run.sh`` gates on ``@job_started``, the last option stamped. The session
init hook keeps a sid that is already set, so the board, the pills and the
attention watcher see the window as any other agent window from its first
second.
"""

from __future__ import annotations

import contextlib
import json
import os
import secrets
import shlex
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

import paths
from board import sweep
from job import agents
from terminal.tmux import JOBS_SESSION_NAME, terminal_process_env

JOBS_SESSION = JOBS_SESSION_NAME

# The MERLIN_* variables a run exports so the job reaches the same Merlin,
# whichever tmux server environment its window was born under. MERLIN_HOME is
# always set; the others are forwarded when the launcher has them. Secrets
# (the SaaS token) are never written into a run script.
FORWARDED_ENV = (
    "MERLIN_DEV",
    "MERLIN_DASHBOARD_PORT",
    "MERLIN_LAUNCH_CWD",
    "MERLIN_ENVIRONMENT_SLUG",
    "MERLIN_FEATURES",
    "MERLIN_SAAS_API",
    "MERLIN_TERMINAL_HOOKS",
)

# How long run.sh waits for the window options before giving up (seconds).
STAMP_WAIT_SECONDS = 30


class LaunchError(RuntimeError):
    """A launch that cannot happen: ``exit_code`` and a message for the run
    log and the history. No window is opened."""

    def __init__(self, exit_code: int, message: str) -> None:
        super().__init__(message)
        self.exit_code = exit_code
        self.message = message


@dataclass
class Run:
    run_id: str
    job_id: str
    job: dict
    trigger: str
    type: str  # "command" | "prompt"
    agent: str | None  # agent id for a prompt job
    cwd: str
    started: float  # epoch seconds
    deadline: float  # seconds after start
    report_mode: str = "always"
    target: str = ""  # session:window_id once the window is open
    dir: Path = field(default_factory=Path)

    @property
    def is_agent(self) -> bool:
        return self.type != "command"

    @property
    def sid(self) -> str:
        return f"job-{self.run_id}"

    @property
    def window_id(self) -> str:
        return self.target.rsplit(":", 1)[-1] if self.target else ""

    def to_meta(self) -> dict:
        return {
            "run_id": self.run_id,
            "job_id": self.job_id,
            "job": self.job,
            "trigger": self.trigger,
            "type": self.type,
            "agent": self.agent,
            "cwd": self.cwd,
            "started": self.started,
            "deadline": self.deadline,
            "report_mode": self.report_mode,
            "target": self.target,
        }

    @classmethod
    def from_meta(cls, meta: dict, directory: Path) -> Run:
        return cls(
            run_id=str(meta.get("run_id") or directory.name),
            job_id=str(meta.get("job_id") or ""),
            job=dict(meta["job"]) if isinstance(meta.get("job"), dict) else {},
            trigger=str(meta.get("trigger") or "manual"),
            type=str(meta.get("type") or "prompt"),
            agent=meta.get("agent"),
            cwd=str(meta.get("cwd") or ""),
            started=float(meta.get("started") or 0.0),
            deadline=float(meta.get("deadline") or 0.0),
            report_mode=str(meta.get("report_mode") or "always"),
            target=str(meta.get("target") or ""),
            dir=directory,
        )


# ---------------------------------------------------------------------------
# The directory
# ---------------------------------------------------------------------------


def runs_dir() -> Path:
    return paths.job_runs_dir()


def run_dirs() -> list[Path]:
    """Every run directory, oldest first by name order (ids are random, so
    the order is only stable, not chronological)."""
    base = runs_dir()
    if not base.exists():
        return []
    return sorted(p for p in base.iterdir() if p.is_dir())


def read_meta(directory: Path) -> Run | None:
    try:
        meta = json.loads((directory / "meta.json").read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(meta, dict):
        return None
    return Run.from_meta(meta, directory)


def write_meta(run: Run) -> None:
    _write_atomic(run.dir / "meta.json", json.dumps(run.to_meta(), indent=2))


def read_exit(directory: Path) -> int | None:
    """The run's exit code from its ``exit`` file, or None while it has
    none (or holds garbage)."""
    try:
        text = (directory / "exit").read_text().strip()
    except OSError:
        return None
    return int(text) if text.lstrip("-").isdigit() else None


def write_exit_once(directory: Path, code: int) -> bool:
    """Publish ``code`` as the run's exit unless one is published already:
    a private temporary file hard-linked to ``exit``, so the first publisher
    wins atomically and a reader never sees a partial code (``run.sh`` does
    the same with ``ln``). True when this call won; the loser reads the
    winning code with ``read_exit``."""
    path = directory / "exit"
    tmp = directory / f".exit.{os.getpid()}.{secrets.token_hex(4)}"
    try:
        tmp.write_text(f"{code}\n")
        os.link(tmp, path)
        return True
    except FileExistsError:
        return False
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink()


def read_pgid(directory: Path) -> int | None:
    """The job's process group, written by ``run.sh``."""
    found = _read_pid_file(directory)
    return found[0] if found else None


def read_process(directory: Path) -> tuple[int, str] | None:
    """``(pgid, start_ticks)`` of the job, written by ``run.sh``: the group
    to signal and the start time of its leader (``/proc/<pid>/stat``, empty
    where there is no procfs), so a pid reused after a reboot is never
    signalled as the job."""
    return _read_pid_file(directory)


def _read_pid_file(directory: Path) -> tuple[int, str] | None:
    try:
        text = (directory / "pid").read_text().strip()
    except OSError:
        return None
    parts = text.split()
    if not parts or not parts[0].isdigit():
        return None
    return int(parts[0]), (parts[1] if len(parts) > 1 else "")


def process_matches(pgid: int, start_ticks: str) -> bool:
    """True when ``pgid``'s leader is alive and, where procfs can say, is the
    process the run started (same start ticks)."""
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return False
    if not start_ticks:
        return True
    now = process_start_ticks(pgid)
    return now is None or now == start_ticks


def process_start_ticks(pid: int) -> str | None:
    """The start ticks of ``pid`` from procfs, None without procfs."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    # The command name sits in parentheses and may hold spaces: split after it.
    rest = stat.rsplit(")", 1)[-1].split()
    return rest[19] if len(rest) > 19 else None


def read_output(directory: Path) -> str:
    try:
        return (directory / "output.log").read_text(errors="replace")
    except OSError:
        return ""


def is_recorded(directory: Path) -> bool:
    """True once history and the run log were materialized for the run."""
    return (directory / "result.json").exists()


def mark_recorded(directory: Path, result: dict) -> None:
    _write_atomic(directory / "result.json", json.dumps(result))


def write_outcome(directory: Path, outcome: dict) -> bool:
    """Publish the canonical outcome of the run, once: no-clobber like the
    exit file. True when this call published it."""
    path = directory / "outcome.json"
    tmp = directory / f".outcome.{os.getpid()}.{secrets.token_hex(4)}"
    try:
        tmp.write_text(json.dumps(outcome))
        os.link(tmp, path)
        return True
    except FileExistsError:
        return False
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink()


def read_outcome(directory: Path) -> dict | None:
    try:
        data = json.loads((directory / "outcome.json").read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def unfinished_run(job_id: str) -> Run | None:
    """The launched run of ``job_id`` that has no outcome yet (its window
    may still be going on without a supervisor), or None. A launch that
    never reached its window (no target) is not a run to adopt."""
    for directory in run_dirs():
        run = read_meta(directory)
        if run is None or run.job_id != job_id or not run.target:
            continue
        if is_recorded(directory) or read_outcome(directory) is not None:
            continue
        return run
    return None


def remove(directory: Path) -> None:
    shutil.rmtree(directory, ignore_errors=True)


def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Creating a run
# ---------------------------------------------------------------------------


def build_prompt(job_id: str, job: dict, trigger: str) -> str:
    """``[Job: id]`` or ``[Triggered by webhook: id]``, a blank line, the
    job's prompt."""
    prompt = job.get("prompt", "")
    if trigger == "webhook":
        return f"[Triggered by webhook: {job_id}]\n\n{prompt}"
    return f"[Job: {job_id}]\n\n{prompt}"


def new_run(
    job_id: str,
    job: dict,
    *,
    trigger: str,
    run_id: str,
    cwd: str,
    deadline: float,
    now: float | None = None,
) -> Run:
    """Create the run directory with its script and prompt. Raises
    ``LaunchError`` when the launch cannot happen (no tmux, the agent's
    binary missing, its state hooks not installed): nothing is written then."""
    if shutil.which("tmux") is None:
        raise LaunchError(1, "tmux is not installed: a job run needs a tmux window.")
    job_type = job.get("type", "prompt")
    agent_id: str | None = None
    if job_type != "command":
        agent = agents.resolve(job)
        problem = agents.launch_problem(agent)
        if problem is not None:
            raise LaunchError(*problem)
        agent_id = agent.id
    run = Run(
        run_id=run_id,
        job_id=job_id,
        job=job,
        trigger=trigger,
        type="command" if job_type == "command" else "prompt",
        agent=agent_id,
        cwd=cwd,
        started=time.time() if now is None else now,
        deadline=deadline,
        report_mode=str(job.get("report_mode") or "always"),
    )
    base = runs_dir()
    base.mkdir(parents=True, exist_ok=True)
    try:
        base.chmod(0o700)
    except OSError:
        pass
    run.dir = base / run_id
    run.dir.mkdir(mode=0o700)
    if run.is_agent:
        prompt = run.dir / "prompt"
        prompt.write_text(build_prompt(job_id, job, trigger))
        prompt.chmod(0o600)
    script = run.dir / "run.sh"
    script.write_text(run_script(run))
    script.chmod(0o700)
    write_meta(run)
    return run


def _exports(run: Run) -> list[str]:
    lines = [
        "unset CLAUDECODE",
        f"export MERLIN_JOB_ID={shlex.quote(run.job_id)}",
        f"export MERLIN_JOB_RUN_ID={shlex.quote(run.run_id)}",
        f"export MERLIN_JOB_TRIGGER={shlex.quote(run.trigger)}",
        f"export MERLIN_HOME={shlex.quote(str(paths.merlin_home()))}",
    ]
    for name in FORWARDED_ENV:
        value = os.environ.get(name)
        if value:
            lines.append(f"export {name}={shlex.quote(value)}")
    return lines


def _job_line(run: Run) -> str:
    """The shell line that runs the job inside the process group: the job,
    then its exit code into ``status`` (that redirection is on the printf
    alone: the job itself keeps the window's terminal)."""
    if run.is_agent:
        return '( launch ); printf \'%s\\n\' "$?" > "$RUN_DIR/status"'
    command = shlex.quote(run.job.get("command", ""))
    return (
        f'bash -lc {command} </dev/null 2>&1 | tee "$RUN_DIR/output.log"; '
        'printf \'%s\\n\' "${PIPESTATUS[0]}" > "$RUN_DIR/status"'
    )


def _launch_function(run: Run) -> str:
    """The ``launch`` function of an agent run: the launch line with the
    prompt read from the run's file. The job-control message fg prints in
    the window then names the function, not the prompt."""
    if not run.is_agent:
        return ""
    agent = agents.AGENTS[run.agent or agents.DEFAULT_AGENT]
    argv = " ".join(shlex.quote(a) for a in agents.launch_argv(agent))
    return f'launch() {{ exec {argv} "$(cat "$RUN_DIR/prompt")"; }}\n'


def run_script(run: Run) -> str:
    """The text of ``run.sh`` for a run."""
    exports = "\n".join(_exports(run))
    job_line = _job_line(run)
    launch_fn = _launch_function(run)
    return f"""#!/bin/bash
# Merlin job run {run.run_id} of job {run.job_id}. Written by Merlin for this run.
RUN_DIR={shlex.quote(str(run.dir))}
win="$TMUX_PANE"

finish() {{
  # Publish the exit no-clobber: a private file hard-linked to exit, so the
  # first publisher (this script, or the supervisor on a deadline) wins and
  # the other reads the winning code back. Both stamp that same code.
  printf '%s\\n' "$1" > "$RUN_DIR/.exit.$$"
  ln "$RUN_DIR/.exit.$$" "$RUN_DIR/exit" 2>/dev/null
  rm -f "$RUN_DIR/.exit.$$"
  code=$(cat "$RUN_DIR/exit" 2>/dev/null)
  case "$code" in ''|*[!0-9]*) code="$1" ;; esac
  if [ -z "$(tmux show-option -wqv -t "$win" @job_exit 2>/dev/null)" ]; then
    tmux set-option -w -t "$win" @job_exit "$code" 2>/dev/null
  fi
  if [ "$(tmux show-option -wqv -t "$win" @agent_state 2>/dev/null)" != done ]; then
    tmux set-option -w -t "$win" @agent_state done 2>/dev/null
  fi
  tmux refresh-client -S 2>/dev/null
  exec "${{SHELL:-/bin/bash}}" -l
}}

# Wait for Merlin to stamp the window (identity, job fields): the job's first
# hook must find the sid already set.
i=0
until [ -n "$(tmux show-option -wqv -t "$win" @job_started 2>/dev/null)" ]; do
  i=$((i + 1))
  if [ "$i" -ge {STAMP_WAIT_SECONDS * 10} ]; then
    echo "merlin: the job window was never stamped, not starting the job" >&2
    finish 1
  fi
  sleep 0.1
done

{exports}
cd {shlex.quote(run.cwd)} || {{ echo "merlin: cannot enter {shlex.quote(run.cwd)}" >&2; finish 1; }}

# The job runs in its own process group (job control), so a deadline can
# end the job and leave this script to record the end. fg hands it the
# terminal, and fg must not be redirected: the job would lose the terminal
# (SIGTTOU) before it starts.
set -m
{launch_fn}{{ {job_line}; }} &
pid=$!
# The group and its leader's start ticks (procfs), so a pid reused later
# is never signalled as this job.
ticks=$(cut -d' ' -f22 "/proc/$pid/stat" 2>/dev/null)
printf '%s %s\\n' "$pid" "$ticks" > "$RUN_DIR/pid"
fg %+
wait "$pid" 2>/dev/null
code=$(cat "$RUN_DIR/status" 2>/dev/null)
case "$code" in ''|*[!0-9]*) code=143 ;; esac
finish "$code"
"""


# ---------------------------------------------------------------------------
# Opening the window
# ---------------------------------------------------------------------------


def launch(run: Run) -> str:
    """Open the run's window, detached, in ``merlin-jobs``, stamp its options
    and return the window target. Raises ``LaunchError`` when the window
    cannot be opened or stamped (the window is closed again then)."""
    env = terminal_process_env(os.environ, term="xterm-256color")
    wid = sweep.open_window(
        JOBS_SESSION,
        run.job_id,
        run.cwd,
        f"bash {shlex.quote(str(run.dir / 'run.sh'))}",
        env=env,
    )
    if not wid:
        raise LaunchError(1, "could not open a tmux window for the run.")
    target = f"{JOBS_SESSION}:{wid}"
    stamped = sweep.set_window_options(
        target,
        {
            "@job_id": run.job_id,
            "@job_run": run.run_id,
            "@job_report": run.report_mode,
            "@agent_sid": run.sid,
            "@agent_cwd": run.cwd,
            "@agent_state": "idle" if run.is_agent else "busy",
            "@job_started": str(int(run.started)),
        },
    )
    if not stamped:
        sweep.kill_window(JOBS_SESSION, wid)
        raise LaunchError(1, "could not stamp the run's tmux window.")
    run.target = target
    write_meta(run)
    return target


def job_windows() -> dict[str, str]:
    """run_id -> ``session:window_id`` of every live job window."""
    rows = sweep.list_window_options(JOBS_SESSION, ("@job_run",))
    if not rows:
        return {}
    return {
        r["@job_run"]: f"{JOBS_SESSION}:{r['window_id']}" for r in rows if r["@job_run"]
    }
