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

import shutil
from dataclasses import dataclass


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


def launch_problem(agent: Agent) -> tuple[int, str] | None:
    """What stops this agent from running right now: ``(exit_code,
    message)``, or None. 127 for a missing binary, 1 for missing hooks,
    the message naming the fix."""
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
