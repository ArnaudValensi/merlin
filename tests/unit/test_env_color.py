"""Tests for the environment color: the palette, the setting through the
settings API, the served favicon, and the page shell (the favicon link and the
sidebar's brand mark)."""

import os
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import env_color
import main as app_mod

ROOT = Path(app_mod.__file__).parent
FAVICON = (ROOT / "static/favicon.svg").read_text()


@pytest.fixture(autouse=True)
def _disable_auth(monkeypatch):
    import auth

    monkeypatch.setattr(app_mod, "DASHBOARD_PASS", "")
    monkeypatch.delenv("MERLIN_SAAS_TOKEN", raising=False)
    monkeypatch.delenv(env_color.KEY, raising=False)
    auth.configure("")


@pytest.fixture
def client():
    with TestClient(app_mod.app) as c:
        yield c


def write_config(tmp_path, text: str) -> None:
    (tmp_path / "config.env").write_text(text)


class TestPalette:
    def test_eight_named_colors_green_first(self):
        assert env_color.NAMES == (
            "green",
            "blue",
            "violet",
            "pink",
            "red",
            "orange",
            "yellow",
            "cyan",
        )
        assert env_color.DEFAULT == "green"
        assert len({hex_ for _, hex_ in env_color.PALETTE}) == 8

    def test_hexes_are_six_digit(self):
        for _, hex_ in env_color.PALETTE:
            assert re.fullmatch(r"#[0-9a-f]{6}", hex_), hex_

    def test_normalize_accepts_any_case_and_spacing(self):
        assert env_color.normalize("Blue") == "blue"
        assert env_color.normalize("  CYAN ") == "cyan"

    def test_normalize_unknown_and_empty_read_as_default(self):
        assert env_color.normalize("") == "green"
        assert env_color.normalize(None) == "green"
        assert env_color.normalize("teal") == "green"
        assert env_color.normalize("#ff0000") == "green"

    def test_accent_of_each_name(self):
        for name, hex_ in env_color.PALETTE:
            assert env_color.accent(name) == hex_
        assert env_color.accent("nope") == env_color.FAVICON_ACCENT

    def test_the_default_accent_is_the_favicon_accent(self):
        assert env_color.accent("green") == "#4ade80"
        assert env_color.FAVICON_ACCENT in FAVICON


class TestSetting:
    def test_reads_config_env_at_call_time(self, tmp_path):
        assert env_color.current() == "green"
        write_config(tmp_path, "DASHBOARD_PASS=x\nMERLIN_ENV_COLOR=violet\n")
        assert env_color.current() == "violet"
        assert env_color.accent(env_color.current()) == "#a78bfa"
        write_config(tmp_path, "MERLIN_ENV_COLOR=Red\n")
        assert env_color.current() == "red"

    def test_unknown_value_in_config_is_the_default(self, tmp_path):
        write_config(tmp_path, "MERLIN_ENV_COLOR=teal\n")
        assert env_color.current() == "green"

    def test_missing_file_is_the_default(self, tmp_path):
        assert not (tmp_path / "config.env").exists()
        assert env_color.current() == "green"

    def test_environment_is_the_fallback_when_config_has_none(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv(env_color.KEY, "orange")
        assert env_color.current() == "orange"
        write_config(tmp_path, "MERLIN_ENV_COLOR=pink\n")
        assert env_color.current() == "pink"

    def test_comments_and_other_keys_are_ignored(self, tmp_path):
        write_config(tmp_path, "# MERLIN_ENV_COLOR=red\nOTHER=blue\n")
        assert env_color.current() == "green"

    def test_last_occurrence_wins_like_the_settings_api(self, tmp_path):
        write_config(tmp_path, "MERLIN_ENV_COLOR=blue\nMERLIN_ENV_COLOR=red\n")
        assert env_color.current() == "red"
        assert app_mod._read_config_env()[env_color.KEY] == "red"


class TestFaviconSvg:
    def test_each_color_substitutes_exactly_the_accent(self):
        accents = FAVICON.count(env_color.FAVICON_ACCENT)
        assert accents == 2  # the mark's fill and the plate's stroke
        for name, hex_ in env_color.PALETTE:
            svg = env_color.favicon_svg(FAVICON, name)
            assert svg.count(hex_) == 2
            assert f'fill="{env_color.PLATE}"' in svg
            if name != "green":
                assert env_color.FAVICON_ACCENT not in svg
            # Nothing but the two accents changed.
            assert svg.replace(hex_, env_color.FAVICON_ACCENT) == FAVICON

    def test_unknown_and_empty_give_the_default(self):
        assert env_color.favicon_svg(FAVICON, "teal") == FAVICON
        assert env_color.favicon_svg(FAVICON, "") == FAVICON
        assert env_color.favicon_svg(FAVICON, None) == FAVICON

    def test_plate_fill_is_not_the_accent(self):
        assert env_color.PLATE.lower() != env_color.FAVICON_ACCENT.lower()
        assert env_color.PLATE not in [h for _, h in env_color.PALETTE]


class TestFaviconRoute:
    def test_default_serves_the_file_as_is(self, client):
        r = client.get("/static/favicon.svg")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("image/svg+xml")
        assert r.headers["cache-control"] == "no-store"
        assert r.text == FAVICON

    @pytest.mark.parametrize("name,hex_", env_color.PALETTE)
    def test_each_palette_color(self, client, tmp_path, name, hex_):
        write_config(tmp_path, f"MERLIN_ENV_COLOR={name}\n")
        r = client.get("/static/favicon.svg")
        assert r.text == env_color.favicon_svg(FAVICON, name)
        assert r.text.count(hex_) == 2
        assert f'fill="{env_color.PLATE}"' in r.text

    def test_query_string_is_a_cache_key_only(self, client, tmp_path):
        write_config(tmp_path, "MERLIN_ENV_COLOR=blue\n")
        assert client.get("/static/favicon.svg?c=red").text.count("#60a5fa") == 2

    def test_unknown_setting_serves_the_default(self, client, tmp_path):
        write_config(tmp_path, "MERLIN_ENV_COLOR=teal\n")
        assert client.get("/static/favicon.svg").text == FAVICON

    def test_change_shows_on_the_next_request(self, client, tmp_path):
        write_config(tmp_path, "MERLIN_ENV_COLOR=blue\n")
        assert "#60a5fa" in client.get("/static/favicon.svg").text
        write_config(tmp_path, "MERLIN_ENV_COLOR=cyan\n")
        assert "#22d3ee" in client.get("/static/favicon.svg").text

    def test_unauthenticated_like_the_rest_of_static(self, monkeypatch):
        import auth

        monkeypatch.setattr(app_mod, "DASHBOARD_PASS", "secret")
        auth.configure("secret")
        with TestClient(app_mod.app) as c:
            r = c.get("/static/favicon.svg", follow_redirects=False)
            assert r.status_code == 200
            assert r.headers["content-type"].startswith("image/svg+xml")

    def test_the_file_on_disk_keeps_the_default_green(self):
        assert FAVICON.count("#4ade80") == 2


class TestSettingsApi:
    @pytest.mark.parametrize("name", env_color.NAMES)
    def test_accepts_every_palette_name(self, client, tmp_path, name):
        write_config(tmp_path, "OTHER=keep\n")
        r = client.post("/api/settings", json={"MERLIN_ENV_COLOR": name})
        assert r.status_code == 200
        assert r.json()["env_color"] == name
        assert r.json()["env_color_hex"] == env_color.accent(name)
        content = (tmp_path / "config.env").read_text()
        assert f"MERLIN_ENV_COLOR={name}\n" in content
        assert "OTHER=keep" in content

    def test_lowercases_and_trims(self, client, tmp_path):
        write_config(tmp_path, "")
        r = client.post("/api/settings", json={"MERLIN_ENV_COLOR": " Violet "})
        assert r.status_code == 200
        assert "MERLIN_ENV_COLOR=violet\n" in (tmp_path / "config.env").read_text()

    @pytest.mark.parametrize("bad", ["teal", "#4ade80", "green;", "gre en", "1"])
    def test_rejects_anything_else(self, client, tmp_path, bad):
        write_config(tmp_path, "MERLIN_ENV_COLOR=blue\n")
        r = client.post("/api/settings", json={"MERLIN_ENV_COLOR": bad})
        assert r.status_code == 422
        assert "MERLIN_ENV_COLOR=blue" in (tmp_path / "config.env").read_text()

    def test_empty_removes_the_key(self, client, tmp_path):
        write_config(tmp_path, "MERLIN_ENV_COLOR=blue\nOTHER=val\n")
        r = client.post("/api/settings", json={"MERLIN_ENV_COLOR": ""})
        assert r.status_code == 200
        assert r.json()["env_color"] == "green"
        content = (tmp_path / "config.env").read_text()
        assert "MERLIN_ENV_COLOR" not in content
        assert "OTHER=val" in content

    def test_save_applies_to_the_process_environment(
        self, client, tmp_path, monkeypatch
    ):
        # config.env is loaded into the environment at boot, and the
        # environment is the fallback when the file has no value: a cleared
        # color must leave it too, or the boot-time value would come back.
        monkeypatch.setenv(env_color.KEY, "blue")
        write_config(tmp_path, "MERLIN_ENV_COLOR=blue\n")
        client.post("/api/settings", json={"MERLIN_ENV_COLOR": "red"})
        assert os.environ[env_color.KEY] == "red"
        assert env_color.current() == "red"
        client.post("/api/settings", json={"MERLIN_ENV_COLOR": ""})
        assert env_color.KEY not in os.environ
        assert env_color.current() == "green"
        assert client.get("/static/favicon.svg").text == FAVICON

    def test_get_reports_the_color_and_the_palette(self, client, tmp_path):
        write_config(tmp_path, "MERLIN_ENV_COLOR=pink\n")
        data = client.get("/api/settings").json()
        assert data["env_color"] == "pink"
        assert [c["name"] for c in data["env_palette"]] == list(env_color.NAMES)
        assert data["env_palette"][0] == {"name": "green", "hex": "#4ade80"}

    def test_get_reads_an_unknown_value_as_the_default(self, client, tmp_path):
        write_config(tmp_path, "MERLIN_ENV_COLOR=teal\n")
        assert client.get("/api/settings").json()["env_color"] == "green"

    def test_api_reports_the_effective_color_from_the_environment(
        self, client, tmp_path, monkeypatch
    ):
        # The one resolver: what the favicon shows is what the API says.
        monkeypatch.setenv(env_color.KEY, "orange")
        assert client.get("/api/settings").json()["env_color"] == "orange"
        assert "#fb923c" in client.get("/static/favicon.svg").text
        # A save of an unrelated setting reports the same effective color.
        write_config(tmp_path, "")
        r = client.post("/api/settings", json={"OPENAI_API_KEY": "sk-x"})
        assert r.json()["env_color"] == "orange"
        assert r.json()["env_color_hex"] == "#fb923c"

    def test_api_and_favicon_agree_on_duplicate_keys(self, client, tmp_path):
        write_config(tmp_path, "MERLIN_ENV_COLOR=blue\nMERLIN_ENV_COLOR=red\n")
        assert client.get("/api/settings").json()["env_color"] == "red"
        assert "#f87171" in client.get("/static/favicon.svg").text
        html = client.get("/settings").text
        checked = re.findall(r'aria-checked="true"[^>]*data-color="(\w+)"', html)
        assert checked == ["red"]

    @pytest.mark.parametrize(
        "bad", [1, 1.5, True, False, 0, ["blue"], [], {"name": "blue"}, {}]
    )
    def test_rejects_non_string_values_and_keeps_the_saved_one(
        self, client, tmp_path, bad
    ):
        write_config(tmp_path, "MERLIN_ENV_COLOR=blue\n")
        r = client.post("/api/settings", json={"MERLIN_ENV_COLOR": bad})
        assert r.status_code == 422
        assert "MERLIN_ENV_COLOR=blue" in (tmp_path / "config.env").read_text()

    def test_null_clears_like_the_empty_string(self, client, tmp_path):
        write_config(tmp_path, "MERLIN_ENV_COLOR=blue\nOTHER=val\n")
        r = client.post("/api/settings", json={"MERLIN_ENV_COLOR": None})
        assert r.status_code == 200
        assert r.json()["env_color"] == "green"
        content = (tmp_path / "config.env").read_text()
        assert "MERLIN_ENV_COLOR" not in content
        assert "OTHER=val" in content

    def test_post_answer_names_and_paints_one_color(
        self, client, tmp_path, monkeypatch
    ):
        # The page paints the answered pair as confirmed, so the name and the
        # hex must come from one resolution, whatever lands between reads.
        write_config(tmp_path, "")
        answers = iter(["blue", "red", "cyan", "pink", "orange"])
        monkeypatch.setattr(env_color, "current", lambda *a: next(answers))
        data = client.post("/api/settings", json={"OPENAI_API_KEY": "sk-x"}).json()
        assert data["env_color"] == "blue"
        assert data["env_color_hex"] == "#60a5fa"

    def test_config_write_is_atomic(self, client, tmp_path, monkeypatch):
        # A reader that opens config.env during a save (the favicon route, a
        # page render) must see the old file or the new one, never a
        # truncated one: the writer fills a sibling temp file and replaces it.
        write_config(tmp_path, "MERLIN_ENV_COLOR=blue\n")
        seen = []
        real_replace = os.replace

        def spying_replace(src, dst):
            seen.append(((tmp_path / "config.env").read_text(), Path(src).parent))
            real_replace(src, dst)

        monkeypatch.setattr(os, "replace", spying_replace)
        client.post("/api/settings", json={"MERLIN_ENV_COLOR": "red"})
        assert seen == [("MERLIN_ENV_COLOR=blue\n", tmp_path)]
        assert (tmp_path / "config.env").read_text() == "MERLIN_ENV_COLOR=red\n"
        assert (tmp_path / "config.env").stat().st_mode & 0o777 == 0o600
        assert list(tmp_path.glob("config.env.*")) == []


class TestPageShell:
    def test_favicon_link_carries_the_color(self, client, tmp_path):
        html = client.get("/terminal").text
        assert 'href="/static/favicon.svg?c=green"' in html
        write_config(tmp_path, "MERLIN_ENV_COLOR=blue\n")
        html = client.get("/terminal").text
        assert 'href="/static/favicon.svg?c=blue"' in html
        assert 'href="/static/favicon.svg?c=green"' not in html

    def test_ico_fallback_is_listed_first_with_a_size(self, client):
        html = client.get("/terminal").text
        ico = html.index('<link rel="icon" href="/static/favicon.ico" sizes="32x32">')
        svg = html.index('<link rel="icon" type="image/svg+xml"')
        assert ico < svg

    def test_brand_mark_carries_the_accent(self, client, tmp_path):
        html = client.get("/terminal").text
        assert 'data-env-color="green" style="--env-color: #4ade80"' in html
        write_config(tmp_path, "MERLIN_ENV_COLOR=orange\n")
        html = client.get("/terminal").text
        assert 'data-env-color="orange" style="--env-color: #fb923c"' in html

    def test_login_page_favicon_link_carries_the_color(self, tmp_path, monkeypatch):
        import auth

        monkeypatch.setattr(app_mod, "DASHBOARD_PASS", "secret")
        auth.configure("secret")
        write_config(tmp_path, "MERLIN_ENV_COLOR=cyan\n")
        with TestClient(app_mod.app) as c:
            html = c.get("/login").text
        assert 'href="/static/favicon.svg?c=cyan"' in html

    def test_only_the_brand_mark_reads_the_color(self):
        css = (ROOT / "static/dashboard.css").read_text()
        assert css.count("var(--env-color") == 2
        block = css[css.index(".sidebar-logo {") : css.index(".sidebar-logo svg")]
        assert block.count("var(--env-color") == 2
        assert "--accent-green: #4ade80" in css

    def test_link_and_mark_come_from_one_resolution(self, client, monkeypatch):
        # The favicon link and the sidebar mark are derived from a single
        # read of the setting per render, so a save landing between two reads
        # can never publish a page whose link and mark disagree.
        original = env_color.current
        answers = iter(["blue", "red", "cyan", "pink"])
        monkeypatch.setattr(env_color, "current", lambda *a: next(answers))
        app_mod.register_template_globals(env_color=env_color.current)
        try:
            html = client.get("/terminal").text
        finally:
            app_mod.register_template_globals(env_color=original)
        assert 'href="/static/favicon.svg?c=blue"' in html
        assert 'data-env-color="blue" style="--env-color: #60a5fa"' in html

    def test_theme_color_stays_the_page_background(self, client, tmp_path):
        write_config(tmp_path, "MERLIN_ENV_COLOR=red\n")
        html = client.get("/terminal").text
        assert 'name="theme-color" content="#0f1117"' in html


class TestSettingsPage:
    def test_shows_the_palette_with_the_current_swatch_selected(self, client, tmp_path):
        write_config(tmp_path, "MERLIN_ENV_COLOR=violet\n")
        html = client.get("/settings").text
        assert "Environment color" in html
        for name, hex_ in env_color.PALETTE:
            assert f'data-color="{name}"' in html
            assert f"--swatch: {hex_}" in html
        checked = re.findall(r'aria-checked="true"[^>]*data-color="(\w+)"', html)
        assert checked == ["violet"]
        assert html.count('aria-checked="false"') == 7

    def test_default_selects_green(self, client):
        html = client.get("/settings").text
        checked = re.findall(r'aria-checked="true"[^>]*data-color="(\w+)"', html)
        assert checked == ["green"]

    def test_shows_the_environment_name(self, client):
        html = client.get("/settings").text
        assert "Environment name" in html


# ---------------------------------------------------------------------------
# M2: the app icons, the manifest, the push icon
# ---------------------------------------------------------------------------


def decode_png(data: bytes) -> tuple[int, int, list[list[tuple[int, int, int]]]]:
    """A small PNG reader for the tests: 8-bit RGB or RGBA, no interlace, the
    five filters. Returns the rows of ``(r, g, b)``."""
    import struct
    import zlib

    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    pos, chunks, width, height, channels = 8, [], 0, 0, 0
    while pos < len(data):
        (length,) = struct.unpack(">I", data[pos : pos + 4])
        kind = data[pos + 4 : pos + 8]
        body = data[pos + 8 : pos + 8 + length]
        if kind == b"IHDR":
            width, height, depth, ctype, _, _, interlace = struct.unpack(
                ">IIBBBBB", body
            )
            assert depth == 8 and interlace == 0
            channels = {2: 3, 6: 4}[ctype]
        elif kind == b"IDAT":
            chunks.append(body)
        pos += 12 + length
    raw = zlib.decompress(b"".join(chunks))
    stride = width * channels
    rows: list[list[tuple[int, int, int]]] = []
    prev = bytearray(stride)
    p = 0
    for _ in range(height):
        f = raw[p]
        line = bytearray(raw[p + 1 : p + 1 + stride])
        p += 1 + stride
        for i in range(stride):
            a = line[i - channels] if i >= channels else 0
            b = prev[i]
            c = prev[i - channels] if i >= channels else 0
            if f == 1:
                line[i] = (line[i] + a) & 0xFF
            elif f == 2:
                line[i] = (line[i] + b) & 0xFF
            elif f == 3:
                line[i] = (line[i] + (a + b) // 2) & 0xFF
            elif f == 4:
                q = a + b - c
                pa, pb, pc = abs(q - a), abs(q - b), abs(q - c)
                pred = a if pa <= pb and pa <= pc else (b if pb <= pc else c)
                line[i] = (line[i] + pred) & 0xFF
        rows.append(
            [tuple(line[i : i + 3]) for i in range(0, stride, channels)]  # type: ignore[misc]
        )
        prev = line
    return width, height, rows


def rgb(hex_: str) -> tuple[int, int, int]:
    return int(hex_[1:3], 16), int(hex_[3:5], 16), int(hex_[5:7], 16)


class TestAppIconRecipe:
    def test_full_bleed_plate_no_border_mark_in_the_safe_zone(self):
        svg = env_color.app_icon_svg(FAVICON, "blue")
        assert '<rect width="512" height="512" fill="#1e2035"/>' in svg
        assert "stroke" not in svg
        assert "rx=" not in svg
        assert 'fill="#60a5fa"' in svg
        assert svg.count("<path") == 1
        # The mark's square, at the recipe's scale and offset, sits inside the
        # inner 80 percent (a circle of diameter 409.6 on a 512 plate).
        side = 512 * env_color.APP_ICON_MARK_SCALE
        offset = (512 - side) / 2
        assert f"translate({offset:g} {offset:g}) scale(0.56)" in svg
        half_diagonal = (side**2 * 2) ** 0.5 / 2
        assert half_diagonal < 512 * 0.8 / 2

    def test_mark_is_the_favicon_path(self):
        svg = env_color.app_icon_svg(FAVICON, "green")
        assert env_color.mark_path(FAVICON) in svg
        assert env_color.mark_path(FAVICON).startswith("M64 416L168.6 180.7")

    def test_unknown_color_renders_the_default(self):
        assert env_color.app_icon_svg(FAVICON, "teal") == env_color.app_icon_svg(
            FAVICON, "green"
        )


class TestCommittedIcons:
    @pytest.mark.parametrize("name", env_color.NAMES)
    def test_every_color_has_its_four_files_at_their_sizes(self, name):
        for file, size in env_color.ICON_FILES:
            path = ROOT / "static" / "icons" / name / file
            assert path.is_file(), f"missing {path}"
            width, height, _ = decode_png(path.read_bytes())
            assert (width, height) == (size, size), path

    def test_the_four_files_are_the_four_the_manifest_and_the_page_use(self):
        assert {f for f, _ in env_color.ICON_FILES} == {
            "icon-192.png",
            "icon-512.png",
            "icon-maskable-512.png",
            "apple-touch-icon.png",
        }

    def test_no_stray_icon_outside_a_color_set(self):
        icons = ROOT / "static" / "icons"
        assert sorted(p.name for p in icons.iterdir() if p.is_dir()) == sorted(
            env_color.NAMES
        )
        assert [p for p in icons.iterdir() if p.is_file()] == []
        assert not (ROOT / "static" / "apple-touch-icon.png").exists()

    @pytest.mark.parametrize("name,hex_", env_color.PALETTE)
    def test_no_border_and_the_accent_in_the_center(self, name, hex_):
        # The plate reaches every edge (no border) and the mark carries the
        # color's accent: read from the pixels of the maskable icon and of the
        # touch icon, the two a mask bites into.
        plate = rgb(env_color.PLATE)
        for file in ("icon-maskable-512.png", "apple-touch-icon.png"):
            path = ROOT / "static" / "icons" / name / file
            w, h, rows = decode_png(path.read_bytes())
            edge = [rows[0][0], rows[0][w - 1], rows[h - 1][0], rows[h - 1][w - 1]]
            edge += [rows[0][w // 2], rows[h - 1][w // 2], rows[h // 2][0]]
            edge += [rows[h // 2][w - 1], rows[h // 8][w // 8], rows[h // 8][w // 2]]
            assert all(px == plate for px in edge), (path, edge)
            # The hat's brim crosses the vertical center line near the bottom
            # of the safe zone: an accent pixel is there.
            column = [rows[y][w // 2] for y in range(h)]
            assert rgb(hex_) in column, path

    def test_committed_icons_match_the_recipe(self):
        """Re-render with rsvg-convert and compare bytes, so a palette or
        favicon change without ``scripts.py render-icons`` fails here. Skipped
        without the tool (an instance, a CI image without librsvg)."""
        import shutil
        import subprocess

        if shutil.which("rsvg-convert") is None:
            pytest.skip("rsvg-convert not installed")
        for name, _ in env_color.PALETTE:
            svg = env_color.app_icon_svg(FAVICON, name).encode()
            for file, size in env_color.ICON_FILES:
                fresh = subprocess.run(
                    ["rsvg-convert", "-w", str(size), "-h", str(size)],
                    input=svg,
                    capture_output=True,
                    check=True,
                ).stdout
                committed = (ROOT / "static" / "icons" / name / file).read_bytes()
                assert fresh == committed, f"{name}/{file} drifted: run render-icons"


class TestManifest:
    @pytest.mark.parametrize("name", env_color.NAMES)
    def test_points_at_the_color_set(self, name):
        m = app_mod.build_manifest("box", name)
        assert [i["src"] for i in m["icons"]] == [
            f"/static/icons/{name}/icon-192.png",
            f"/static/icons/{name}/icon-512.png",
            f"/static/icons/{name}/icon-maskable-512.png",
        ]
        assert m["theme_color"] == "#0f1117"
        assert m["background_color"] == "#0f1117"

    def test_no_color_and_an_unknown_one_point_at_green(self):
        assert app_mod.build_manifest("box")["icons"][0]["src"].startswith(
            "/static/icons/green/"
        )
        assert app_mod.build_manifest("box", "teal")["icons"][0]["src"].startswith(
            "/static/icons/green/"
        )

    def test_route_reads_the_setting_and_serves_the_files(self, client, tmp_path):
        write_config(tmp_path, "MERLIN_ENV_COLOR=pink\n")
        body = client.get("/manifest.webmanifest?c=pink").json()
        for icon in body["icons"]:
            assert icon["src"].startswith("/static/icons/pink/")
            assert client.get(icon["src"]).status_code == 200
        write_config(tmp_path, "MERLIN_ENV_COLOR=cyan\n")
        body = client.get("/manifest.webmanifest").json()
        assert body["icons"][0]["src"] == "/static/icons/cyan/icon-192.png"


class TestPageLinks:
    def test_manifest_and_touch_icon_follow_the_color(self, client, tmp_path):
        write_config(tmp_path, "MERLIN_ENV_COLOR=violet\n")
        html = client.get("/terminal").text
        assert '<link rel="manifest" href="/manifest.webmanifest?c=violet">' in html
        assert (
            '<link rel="apple-touch-icon" href="/static/icons/violet/apple-touch-icon.png">'
            in html
        )
        assert (
            client.get("/static/icons/violet/apple-touch-icon.png").status_code == 200
        )

    def test_login_touch_icon_follows_the_color(self, tmp_path, monkeypatch):
        import auth

        monkeypatch.setattr(app_mod, "DASHBOARD_PASS", "secret")
        auth.configure("secret")
        write_config(tmp_path, "MERLIN_ENV_COLOR=red\n")
        with TestClient(app_mod.app) as c:
            html = c.get("/login").text
        assert 'href="/static/icons/red/apple-touch-icon.png"' in html


class TestPushIcon:
    def test_payload_icon_follows_the_color(self, tmp_path):
        from board.sweep import Window
        from notifications.push import build_payload
        from notifications.watcher import Watcher

        def event():
            w = Watcher(lambda: [], machine="box")
            (ev,) = w.observe(
                [
                    Window(
                        sid="s1",
                        state="done",
                        cwd="/h/u/proj",
                        parent="",
                        relation="",
                        session="alpha",
                        window_id="@1",
                        index=1,
                        active=False,
                        activity=0,
                        name="claude",
                    )
                ]
            )
            return ev

        assert build_payload(event())["icon"] == "/static/icons/green/icon-192.png"
        write_config(tmp_path, "MERLIN_ENV_COLOR=orange\n")
        assert build_payload(event())["icon"] == "/static/icons/orange/icon-192.png"
        assert (ROOT / "static/icons/orange/icon-192.png").is_file()

    def test_worker_default_is_a_committed_file(self):
        src = (ROOT / "notifications/static/sw.js").read_text()
        assert "data.icon || '/static/icons/green/icon-192.png'" in src
        assert (ROOT / "static/icons/green/icon-192.png").is_file()
