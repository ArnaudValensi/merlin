"""Shared plumbing for the ``merlin app`` commands. Standard library only.

Every command prints JSON on stdout (``logs`` prints the log), exits 0 on
success, 1 on a runtime error and 2 on a usage error, with the message on
stderr.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from typing import Any, NoReturn

from app.sessions import AppError


def parser(prog: str, doc: str | None) -> argparse.ArgumentParser:
    summary, _, epilog = (doc or "").strip().partition("\n")
    return argparse.ArgumentParser(
        prog=f"merlin app {prog}",
        description=summary,
        epilog=epilog.strip() or None,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )


def emit(data: Any) -> None:
    json.dump(data, sys.stdout, indent=2)
    sys.stdout.write("\n")


def fail(message: str, code: int = 1) -> NoReturn:
    print(f"error: {message}", file=sys.stderr)
    sys.exit(code)


def run(main: Callable[[], int | None]) -> NoReturn:
    try:
        code = main() or 0
    except KeyError as exc:
        fail(f"No app named '{exc.args[0]}'. See: merlin app list")
    except ValueError as exc:
        fail(str(exc), code=2)
    except AppError as exc:
        fail(str(exc))
    except KeyboardInterrupt:
        code = 130
    sys.exit(code)
