"""Prerequisites for app streaming. Standard library only.

Two groups: what running an app needs (Xvfb, xdotool, ImageMagick ``import``)
and what streaming it needs (system Python with GStreamer WebRTC and
python-xlib, plus a few GStreamer elements). GPU rendering and H.264 encoding
are optional: their absence only degrades quality.
"""

from __future__ import annotations

import functools
import json
import platform
import shutil
import subprocess
from dataclasses import asdict, dataclass

SYSTEM_PYTHON = "/usr/bin/python3"
ARCH_INSTALL = "sudo pacman -S xorg-server-xvfb virtualgl xdotool python-xlib"

TOOLS = {
    "Xvfb": "xorg-server-xvfb",
    "xdotool": "xdotool",
    "import": "imagemagick",
    "vglrun": "virtualgl",
}

# Checked inside the system Python, the one streamer.py runs under.
_PROBE = r"""
import json
out = {"gi": False, "xlib": False, "elements": {}}
try:
    import gi
    gi.require_version("Gst", "1.0")
    gi.require_version("GstWebRTC", "1.0")
    gi.require_version("GstSdp", "1.0")
    from gi.repository import Gst, GstWebRTC, GstSdp  # noqa: F401
    Gst.init(None)
    out["gi"] = True
    for name in ("webrtcbin", "ximagesrc", "vp8enc", "nvh264enc", "openh264enc"):
        out["elements"][name] = Gst.ElementFactory.find(name) is not None
except Exception:
    pass
try:
    import Xlib.ext.xtest  # noqa: F401
    out["xlib"] = True
except Exception:
    pass
print(json.dumps(out))
"""


@dataclass
class Check:
    name: str
    ok: bool
    required: bool
    hint: str


@functools.cache
def _probe_python() -> dict:
    try:
        result = subprocess.run(
            [SYSTEM_PYTHON, "-c", _PROBE],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        return json.loads(result.stdout.strip().splitlines()[-1])
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return {"gi": False, "xlib": False, "elements": {}}


def is_linux() -> bool:
    return platform.system() == "Linux"


def checks() -> list[Check]:
    """Every prerequisite, required ones first."""
    if not is_linux():
        return [Check("Linux", False, True, "App streaming runs on Linux only")]
    probe = _probe_python()
    elements = probe.get("elements", {})
    result = [
        Check("Xvfb", bool(shutil.which("Xvfb")), True, "xorg-server-xvfb"),
        Check("xdotool", bool(shutil.which("xdotool")), True, "xdotool"),
        Check("ImageMagick import", bool(shutil.which("import")), True, "imagemagick"),
        Check(
            "GStreamer for Python",
            bool(probe.get("gi")),
            True,
            "python-gobject, gst-plugins-base, gst-plugins-bad",
        ),
        Check("python-xlib", bool(probe.get("xlib")), True, "python-xlib"),
    ]
    for element, package in (
        ("webrtcbin", "gst-plugins-bad"),
        ("ximagesrc", "gst-plugins-good"),
        ("vp8enc", "gst-plugins-good"),
    ):
        result.append(Check(element, bool(elements.get(element)), True, package))
    result += [
        Check(
            "GPU rendering (VirtualGL)",
            bool(shutil.which("vglrun")),
            False,
            "virtualgl; without it apps render in software",
        ),
        Check(
            "H.264 encoder",
            bool(elements.get("nvh264enc") or elements.get("openh264enc")),
            False,
            "gst-plugins-bad (nvh264enc) or openh264; without it video is VP8",
        ),
    ]
    return result


def missing_required() -> list[Check]:
    return [check for check in checks() if check.required and not check.ok]


def install_hint() -> str:
    if shutil.which("pacman"):
        return ARCH_INSTALL
    return "Install the missing packages with your distribution's package manager."


def missing_message(tools: list[str]) -> str:
    """Error text for CLI commands that need specific tools."""
    if not is_linux():
        return "App streaming runs on Linux only."
    missing = [tool for tool in tools if shutil.which(tool) is None]
    if not missing:
        return "Missing prerequisites."
    packages = ", ".join(TOOLS.get(tool, tool) for tool in missing)
    return (
        f"Missing {', '.join(missing)} (package: {packages}). Install: {install_hint()}"
    )


def report() -> dict:
    """Everything the Apps page needs to show the prerequisites checklist."""
    all_checks = checks()
    return {
        "ok": not any(c.required and not c.ok for c in all_checks),
        "checks": [asdict(c) for c in all_checks],
        "install": install_hint(),
    }
