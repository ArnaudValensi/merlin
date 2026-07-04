"""Tests for graceful degradation when optional deps are missing."""

from pathlib import Path
from unittest import mock

import pytest


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
# Nav item disabled state
# ---------------------------------------------------------------------------


class TestNavItemDisabling:
    def test_tmux_missing_disables_terminal_nav(self):
        import main

        # Save original state
        orig_tmux = main.TMUX_AVAILABLE
        orig_items = [dict(item) for item in main.nav_items]

        try:
            # Reset terminal nav item
            for item in main.nav_items:
                if item.get("url") == "/terminal":
                    item.pop("disabled", None)
                    item.pop("tooltip", None)

            with mock.patch("shutil.which") as m:
                m.side_effect = lambda cmd: None if cmd == "tmux" else f"/usr/bin/{cmd}"
                main._check_optional_deps()

            terminal_item = next(
                i for i in main.nav_items if i.get("url") == "/terminal"
            )
            assert terminal_item.get("disabled") is True
            assert "tmux" in terminal_item.get("tooltip", "").lower()
        finally:
            # Restore state
            main.TMUX_AVAILABLE = orig_tmux
            main.nav_items[:] = orig_items

    def test_tmux_present_keeps_terminal_enabled(self):
        import main

        orig_tmux = main.TMUX_AVAILABLE
        orig_items = [dict(item) for item in main.nav_items]

        try:
            for item in main.nav_items:
                if item.get("url") == "/terminal":
                    item.pop("disabled", None)

            with mock.patch("shutil.which", return_value="/usr/bin/tmux"):
                main._check_optional_deps()

            terminal_item = next(
                i for i in main.nav_items if i.get("url") == "/terminal"
            )
            assert terminal_item.get("disabled") is not True
        finally:
            main.TMUX_AVAILABLE = orig_tmux
            main.nav_items[:] = orig_items


# ---------------------------------------------------------------------------
# Terminal route with tmux missing
# ---------------------------------------------------------------------------


class TestTerminalRouteTmuxMissing:
    def test_tmux_available_flag_set_to_false(self):
        """When tmux is missing, TMUX_AVAILABLE is set to False."""
        import main

        orig = main.TMUX_AVAILABLE
        orig_items = [dict(item) for item in main.nav_items]

        try:
            main.TMUX_AVAILABLE = True
            with mock.patch("shutil.which") as m:
                m.side_effect = lambda cmd: None if cmd == "tmux" else f"/usr/bin/{cmd}"
                main._check_optional_deps()
            assert main.TMUX_AVAILABLE is False
        finally:
            main.TMUX_AVAILABLE = orig
            main.nav_items[:] = orig_items

    def test_terminal_route_checks_tmux(self):
        """Terminal route returns 503 when TMUX_AVAILABLE is False."""
        import main
        from terminal.routes import terminal_page
        from unittest.mock import MagicMock

        orig = main.TMUX_AVAILABLE
        try:
            main.TMUX_AVAILABLE = False
            request = MagicMock()
            response = terminal_page(request)
            assert response.status_code == 503
            assert "tmux" in response.body.decode().lower()
        finally:
            main.TMUX_AVAILABLE = orig


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
