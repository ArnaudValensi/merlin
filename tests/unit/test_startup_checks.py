"""The checks Merlin runs before it starts: the package manager hints, the
tmux 3.8 requirement (a fake ``tmux`` first on PATH, so no test reaches the
real default server) and the order of ``_validate_config``."""

import os
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# Package manager detection
# ---------------------------------------------------------------------------


class TestDetectPkgManager:
    def test_detects_apt(self):
        from main import _detect_pkg_manager

        with mock.patch("shutil.which") as m:
            m.side_effect = lambda cmd: "/usr/bin/apt" if cmd == "apt" else None
            assert _detect_pkg_manager() == "apt"

    def test_detects_pacman(self):
        from main import _detect_pkg_manager

        with mock.patch("shutil.which") as m:
            m.side_effect = lambda cmd: "/usr/bin/pacman" if cmd == "pacman" else None
            assert _detect_pkg_manager() == "pacman"

    def test_detects_brew(self):
        from main import _detect_pkg_manager

        with mock.patch("shutil.which") as m:
            m.side_effect = lambda cmd: "/usr/local/bin/brew" if cmd == "brew" else None
            assert _detect_pkg_manager() == "brew"

    def test_returns_empty_when_none(self):
        from main import _detect_pkg_manager

        with mock.patch("shutil.which", return_value=None):
            assert _detect_pkg_manager() == ""


class TestInstallCmd:
    def test_apt_command(self):
        from main import _install_cmd

        with mock.patch("main._detect_pkg_manager", return_value="apt"):
            assert _install_cmd("tmux") == "sudo apt install -y tmux"

    def test_pacman_command(self):
        from main import _install_cmd

        with mock.patch("main._detect_pkg_manager", return_value="pacman"):
            assert _install_cmd("tmux") == "sudo pacman -S --noconfirm tmux"

    def test_brew_command(self):
        from main import _install_cmd

        with mock.patch("main._detect_pkg_manager", return_value="brew"):
            assert _install_cmd("tmux") == "brew install tmux"

    def test_fallback_when_no_pkg_manager(self):
        from main import _install_cmd

        with mock.patch("main._detect_pkg_manager", return_value=""):
            result = _install_cmd("tmux")
            assert "tmux" in result


# ---------------------------------------------------------------------------
# The tmux check at launch
# ---------------------------------------------------------------------------

NEED = "Merlin needs tmux 3.8 or newer."


NO_SERVER_ENOENT = (
    "error connecting to /tmp/tmux-1000/default (No such file or directory)"
)
NO_SERVER_REFUSED = "no server running on /tmp/tmux-1000/default"
SLEEP = shutil.which("sleep") or "/bin/sleep"


def install_fake_tmux(
    tmp_path: Path,
    monkeypatch,
    *,
    version: str | None,
    server: str | None = None,
    server_error: str = NO_SERVER_ENOENT,
    hang: str | None = None,
) -> Path:
    """A ``tmux`` first on PATH that answers ``-V`` with ``version`` (None:
    no tmux on PATH at all) and ``list-sessions`` with ``server`` (None: exit
    1 with ``server_error`` on stderr, what tmux prints when there is no
    server unless the test says otherwise). Every call is appended to
    ``calls.log`` with the TMUX it saw. ``hang`` names the command that
    sleeps instead of answering (``"-V"`` or ``"list-sessions"``)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.log"
    if version is not None:
        body = f'#!/bin/sh\necho "$* TMUX=${{TMUX-unset}}" >> "{log}"\n'
        body += 'case "$1" in\n'
        if hang == "-V":
            body += f"  -V) exec {SLEEP} 30 ;;\n"
        else:
            body += f'  -V) echo "{version}"; exit 0 ;;\n'
        if hang == "list-sessions":
            body += f"  list-sessions) exec {SLEEP} 30 ;;\n"
        elif server is None:
            body += f'  list-sessions) echo "{server_error}" >&2; exit 1 ;;\n'
        else:
            body += f'  list-sessions) echo "{server}"; exit 0 ;;\n'
        body += "esac\nexit 1\n"
        fake = bin_dir / "tmux"
        fake.write_text(body)
        fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    # Only the fake directory: the real tmux (and its guard shim) are out of
    # reach, so "no tmux" is really no tmux.
    monkeypatch.setenv("PATH", str(bin_dir))
    return log


def refusal(capsys) -> str:
    return capsys.readouterr().err


class TestTmuxLaunchCheck:
    def test_missing_tmux_refuses_with_the_install_command(
        self, tmp_path, monkeypatch, capsys
    ):
        import main

        install_fake_tmux(tmp_path, monkeypatch, version=None)
        monkeypatch.setattr(
            main, "_install_cmd", lambda pkg: f"sudo apt install -y {pkg}"
        )
        with pytest.raises(SystemExit) as exc:
            main._check_tmux()
        assert exc.value.code == 1
        err = refusal(capsys)
        assert f"tmux is not installed. {NEED}" in err
        assert "sudo apt install -y tmux" in err

    @pytest.mark.parametrize("version", ["tmux 3.4", "tmux 3.7c"])
    def test_an_old_binary_refuses_and_names_the_version(
        self, tmp_path, monkeypatch, capsys, version
    ):
        import main

        install_fake_tmux(tmp_path, monkeypatch, version=version)
        with pytest.raises(SystemExit) as exc:
            main._check_tmux()
        assert exc.value.code == 1
        err = refusal(capsys)
        assert f"{version} is too old. {NEED}" in err
        assert "Homebrew" in err and "getting-started.md#tmux-38-or-newer" in err

    @pytest.mark.parametrize("version", ["tmux 3.8", "tmux next-3.9", "tmux master"])
    @pytest.mark.parametrize(
        "no_server", [NO_SERVER_ENOENT, NO_SERVER_REFUSED], ids=["no-socket", "stale"]
    )
    def test_a_supported_binary_with_no_server_starts(
        self, tmp_path, monkeypatch, capsys, version, no_server
    ):
        """No server: no socket (ENOENT), or a stale socket nobody listens
        on (ECONNREFUSED), the two ways tmux says it (tmux/client.c)."""
        import main

        log = install_fake_tmux(
            tmp_path, monkeypatch, version=version, server_error=no_server
        )
        monkeypatch.setenv("TMUX", "/tmp/tmux-0/other,1,0")
        main._check_tmux()  # no exit
        assert refusal(capsys) == ""
        calls = log.read_text().splitlines()
        assert calls[0].startswith("-V ")
        assert calls[1].startswith("list-sessions -F #{version} ")
        assert len(calls) == 2
        # Read-only, and the default server even when Merlin was started
        # from inside a tmux pane.
        for call in calls:
            assert "new-session" not in call and "kill-server" not in call
            assert "attach" not in call and "start-server" not in call
            assert call.endswith("TMUX=unset")

    def test_a_binary_that_hangs_reads_as_unknown_and_refuses(
        self, tmp_path, monkeypatch, capsys
    ):
        """The fake sleeps (an absolute sleep: PATH holds the fake alone) and
        the probe's timeout ends it: the refusal names the timeout, and the
        check took at least that long and not the sleep's 30 seconds."""
        import main

        log = install_fake_tmux(tmp_path, monkeypatch, version="tmux 3.8", hang="-V")
        monkeypatch.setattr(main, "TMUX_PROBE_TIMEOUT", 0.5)
        started = time.monotonic()
        with pytest.raises(SystemExit) as exc:
            main._check_tmux()
        elapsed = time.monotonic() - started
        assert exc.value.code == 1
        assert 0.5 <= elapsed < 10
        err = refusal(capsys)
        assert "tmux -V gave no answer in 0.5 seconds" in err and NEED in err
        assert log.read_text().splitlines() == ["-V TMUX=unset"]

    def test_a_binary_whose_version_command_fails_refuses(
        self, tmp_path, monkeypatch, capsys
    ):
        import main

        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        fake = bin_dir / "tmux"
        fake.write_text('#!/bin/sh\necho "tmux: unknown option -- V" >&2\nexit 1\n')
        fake.chmod(0o755)
        monkeypatch.setenv("PATH", str(bin_dir))
        with pytest.raises(SystemExit) as exc:
            main._check_tmux()
        assert exc.value.code == 1
        err = refusal(capsys)
        assert "tmux -V failed: tmux: unknown option -- V" in err and NEED in err

    def test_a_server_probe_that_fails_for_another_reason_refuses(
        self, tmp_path, monkeypatch, capsys
    ):
        """A server is there but tmux could not read it (a protocol mismatch
        between an upgraded client and the old server, say): not "no server"."""
        import main

        install_fake_tmux(
            tmp_path,
            monkeypatch,
            version="tmux 3.8",
            server_error="protocol version mismatch (client 8, server 7)",
        )
        with pytest.raises(SystemExit) as exc:
            main._check_tmux()
        assert exc.value.code == 1
        err = refusal(capsys)
        assert (
            "The tmux server could not be checked (tmux list-sessions"
            " failed: protocol version mismatch (client 8, server 7))."
        ) in err
        assert NEED in err and "tmux kill-server" in err

    def test_a_socket_tmux_cannot_open_refuses_with_its_words(
        self, tmp_path, monkeypatch, capsys
    ):
        """Seen for real: a TMUX_TMPDIR too long for a unix socket. Not "no
        server" (ENOENT, ECONNREFUSED), so the start is refused, quoting tmux."""
        import main

        install_fake_tmux(
            tmp_path,
            monkeypatch,
            version="tmux 3.8",
            server_error="error connecting to /very/long/path/tmux-1000/default (File name too long)",
        )
        with pytest.raises(SystemExit) as exc:
            main._check_tmux()
        assert exc.value.code == 1
        err = refusal(capsys)
        assert "The tmux server could not be checked (tmux list-sessions failed:" in err
        assert "(File name too long))." in err

    def test_a_server_probe_that_hangs_refuses(self, tmp_path, monkeypatch, capsys):
        import main

        log = install_fake_tmux(
            tmp_path, monkeypatch, version="tmux 3.8", hang="list-sessions"
        )
        monkeypatch.setattr(main, "TMUX_PROBE_TIMEOUT", 0.5)
        started = time.monotonic()
        with pytest.raises(SystemExit) as exc:
            main._check_tmux()
        elapsed = time.monotonic() - started
        assert exc.value.code == 1
        assert 0.5 <= elapsed < 10
        err = refusal(capsys)
        assert (
            "The tmux server could not be checked (tmux list-sessions"
            " gave no answer in 0.5 seconds)."
        ) in err
        assert len(log.read_text().splitlines()) == 2

    def test_a_running_server_older_than_the_binary_refuses(
        self, tmp_path, monkeypatch, capsys
    ):
        import main

        install_fake_tmux(tmp_path, monkeypatch, version="tmux 3.8", server="3.4")
        with pytest.raises(SystemExit) as exc:
            main._check_tmux()
        assert exc.value.code == 1
        err = refusal(capsys)
        assert f"A tmux 3.4 server is still running. {NEED}" in err
        assert "tmux kill-server" in err

    def test_a_running_server_that_reports_no_version_refuses(
        self, tmp_path, monkeypatch, capsys
    ):
        import main

        # list-sessions succeeds with an empty line: a server too old to
        # know the version format at all.
        install_fake_tmux(tmp_path, monkeypatch, version="tmux 3.8", server="")
        with pytest.raises(SystemExit) as exc:
            main._check_tmux()
        assert exc.value.code == 1
        assert "does not report its version" in refusal(capsys)

    def test_a_running_supported_server_starts(self, tmp_path, monkeypatch, capsys):
        import main

        install_fake_tmux(tmp_path, monkeypatch, version="tmux 3.8", server="3.8")
        main._check_tmux()
        assert refusal(capsys) == ""

    def test_the_refusal_reaches_the_server_log(self, tmp_path, monkeypatch, capsys):
        """A start through `merlin restart` or a service shows the reason in
        merlin.log too, once, beside the stderr line."""
        import logging

        import main

        install_fake_tmux(tmp_path, monkeypatch, version="tmux 3.4")
        log_file = tmp_path / "merlin.log"
        handler = logging.FileHandler(log_file)
        main.logger.addHandler(handler)
        try:
            with pytest.raises(SystemExit):
                main._check_tmux()
        finally:
            main.logger.removeHandler(handler)
            handler.close()
        assert "Start refused: tmux 3.4 is too old." in log_file.read_text()
        assert refusal(capsys).count("is too old") == 1

    def test_one_minimum_and_one_parser(self):
        """No second copy of 3.8 or of the version parser in main.py."""
        source = (ROOT / "main.py").read_text()
        assert "3, 8" not in source and '"3.8"' not in source
        assert "parse_tmux_version" in source and "MIN_TMUX" in source

    def test_a_cli_command_that_does_not_start_the_server_is_not_gated(
        self, tmp_path, monkeypatch
    ):
        """`merlin version` with a tmux 3.4 first on PATH still works (A.5):
        only the server's start runs the check."""
        install_fake_tmux(tmp_path, monkeypatch, version="tmux 3.4")
        env = dict(os.environ)
        env["PATH"] = f"{tmp_path / 'bin'}{os.pathsep}{os.path.dirname(sys.executable)}"
        result = subprocess.run(
            [sys.executable, "cli.py", "version"],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stderr
        assert "too old" not in result.stderr


# ---------------------------------------------------------------------------
# _validate_config ordering
# ---------------------------------------------------------------------------


class TestValidateConfigOrdering:
    def test_missing_config_exits_immediately(self):
        """When config file is missing, exit immediately with a setup hint."""
        import main

        orig_pass = main.DASHBOARD_PASS
        orig_token = main.MERLIN_SAAS_TOKEN
        try:
            main.DASHBOARD_PASS = ""
            main.MERLIN_SAAS_TOKEN = ""
            with mock.patch(
                "main.paths.config_path", return_value=Path("/nonexistent/config.env")
            ):
                with pytest.raises(SystemExit) as exc_info:
                    main._validate_config()
                assert exc_info.value.code == 1
        finally:
            main.DASHBOARD_PASS = orig_pass
            main.MERLIN_SAAS_TOKEN = orig_token

    def test_tmux_is_checked_after_fd_and_before_anything_starts(
        self, tmp_path, monkeypatch
    ):
        import main

        order: list[str] = []
        monkeypatch.setattr(main, "_check_fd", lambda: order.append("fd"))
        monkeypatch.setattr(main, "_check_tmux", lambda: order.append("tmux"))
        monkeypatch.setattr(main, "MERLIN_SAAS_TOKEN", "token")
        main._validate_config()
        assert order == ["fd", "tmux"]
