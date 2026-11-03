"""The agents a job can run, as one small table.

An agent job runs the agent's real interactive interface in a tmux window
(see ``job/runs.py``), with every permission check bypassed: a job runs
unattended by definition, and a permission dialog nobody answers would hold
it until its deadline. The table is the seam for a future agent: its id, the
binary on ``PATH``, the launch argv (the prompt is appended as the last
argument, read from the run's prompt file by ``run.sh``), and the hook
provider whose state hooks (``lib/skills.py``) must be installed for the
supervisor to see the run finish.
"""

from __future__ import annotations

import os
import shutil
import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Agent:
    id: str
    binary: str
    argv: tuple[str, ...]  # the launch line, before the prompt
    hooks: str  # the hook provider in lib/skills.py ("claude" or "codex")
    label: str


AGENTS: dict[str, Agent] = {
    "claude": Agent(
        id="claude",
        binary="claude",
        argv=("claude", "--dangerously-skip-permissions"),
        hooks="claude",
        label="Claude Code",
    ),
    "codex": Agent(
        id="codex",
        binary="codex",
        argv=("codex", "--dangerously-bypass-approvals-and-sandbox"),
        hooks="codex",
        label="Codex",
    ),
}

DEFAULT_AGENT = "claude"

# What a job's creator sees (form help text, the CLI's stderr, the docs).
BYPASS_WARNING = (
    "Agent jobs run with every permission check bypassed "
    "(claude --dangerously-skip-permissions, codex "
    "--dangerously-bypass-approvals-and-sandbox): the agent edits files and "
    "runs commands without asking. Give a job only work you would let run "
    "unattended."
)


def agent_ids() -> list[str]:
    return list(AGENTS)


def resolve(job: dict) -> Agent:
    """The agent of a job (``agent`` field, default ``claude``). An unknown
    id falls back to the default, so an old or hand-edited job still runs."""
    return AGENTS.get(str(job.get("agent") or DEFAULT_AGENT), AGENTS[DEFAULT_AGENT])


def launch_argv(agent: Agent) -> list[str]:
    """The launch line without the prompt (``run.sh`` appends it)."""
    return list(agent.argv)


def binary_missing(agent: Agent) -> bool:
    return shutil.which(agent.binary) is None


def hooks_missing(agent: Agent) -> bool:
    """True when Merlin's state hooks are not installed in the agent's user
    config. Read-only, whatever the consent mode says: a job needs
    ``@agent_state`` to know when its run finished, so without the hooks a
    launch is refused up front (``launch_problem``)."""
    from lib.skills import state_hooks_present

    return not state_hooks_present(agent.hooks)


def codex_distrusts(cwd: str) -> bool:
    """True when Codex's config does not trust ``cwd``: the nearest entry of
    its ``[projects]`` table, from the directory up, is not ``trusted``, or
    there is none. Codex runs no hook in such a directory. A config that
    cannot be read proves nothing, so it does not count as distrust."""
    home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    try:
        projects = tomllib.loads((home / "config.toml").read_text()).get("projects")
    except FileNotFoundError:
        return True
    except (OSError, ValueError):
        return False
    if not isinstance(projects, dict):
        return True
    path = Path(os.path.realpath(cwd))
    for directory in (path, *path.parents):
        entry = projects.get(str(directory))
        if isinstance(entry, dict) and "trust_level" in entry:
            return entry["trust_level"] != "trusted"
    return True


def launch_problem(agent: Agent, cwd: str | None = None) -> tuple[int, str] | None:
    """What stops this agent from running right now: ``(exit_code,
    message)``, or None. 127 for a missing binary, 1 for missing hooks or,
    given the run's ``cwd``, a directory Codex does not trust (no hook would
    fire and the run would only end on its deadline). The message names the
    fix."""
    if binary_missing(agent):
        return 127, (
            f"{agent.label} is not installed: '{agent.binary}' was not found on "
            "PATH, so the job cannot start."
        )
    if hooks_missing(agent):
        return 1, (
            f"Merlin's state hooks are not installed for {agent.label}, so a job "
            "run could not be seen finishing. Install them with: "
            "merlin config agent-state-hooks auto"
        )
    if cwd and agent.id == "codex" and codex_distrusts(cwd):
        return 1, (
            f"Codex does not trust {cwd}, and runs no hook in a directory it does "
            "not trust, so a job run could not be seen finishing. Open codex once "
            "in that directory and trust it, or point the job at a trusted "
            "project."
        )
    return None


def problems() -> dict[str, str]:
    """Agent id -> the message of its launch problem, for the agents that
    have one. The job form shows it up front."""
    out: dict[str, str] = {}
    for agent_id, agent in AGENTS.items():
        problem = launch_problem(agent)
        if problem is not None:
            out[agent_id] = problem[1]
    return out
