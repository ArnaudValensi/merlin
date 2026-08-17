"""Responsive interaction checks for the fixture-driven Timeline page."""

import os
import signal
import socket
import subprocess
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import pytest

pytest.importorskip("playwright")

from playwright.sync_api import sync_playwright


def _free_port():
    with socket.socket() as sock:
        sock.bind(("", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def timeline_server(tmp_path_factory):
    port = _free_port()
    merlin_home = tmp_path_factory.mktemp("timeline-home")
    provider_home = tmp_path_factory.mktemp("timeline-provider-home")
    merlin_home.joinpath("config.env").write_text("MERLIN_ACTIVITY_HOOKS=ask\n")
    env = os.environ.copy()
    env.update(
        {
            "DASHBOARD_PASS": "",
            "DISCORD_BOT_TOKEN": "",
            "DISCORD_CHANNEL_IDS": "",
            "MERLIN_SAAS_TOKEN": "",
            "MERLIN_TIMELINE_FIXTURES": "1",
            "MERLIN_ACTIVITY_HOOKS": "ask",
            "MERLIN_HOME": str(merlin_home),
            "CLAUDE_CONFIG_DIR": str(provider_home / "claude"),
            "CODEX_HOME": str(provider_home / "codex"),
        }
    )
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    process = subprocess.Popen(
        ["uv", "run", "main.py", "--port", str(port)],
        cwd=root,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{port}"
    for _ in range(40):
        try:
            import urllib.request

            urllib.request.urlopen(f"{url}/timeline", timeout=1)
            break
        except Exception:
            time.sleep(0.25)
    else:
        process.kill()
        raise RuntimeError("Timeline test server failed to start")
    yield url
    process.send_signal(signal.SIGTERM)
    process.wait(timeout=5)


@pytest.fixture(scope="module")
def real_timeline_server(tmp_path_factory):
    port = _free_port()
    merlin_home = tmp_path_factory.mktemp("timeline-real-home")
    provider_home = tmp_path_factory.mktemp("timeline-real-provider-home")
    merlin_home.joinpath("config.env").write_text("MERLIN_ACTIVITY_HOOKS=off\n")
    env = os.environ.copy()
    env.update(
        {
            "DASHBOARD_PASS": "",
            "DISCORD_BOT_TOKEN": "",
            "DISCORD_CHANNEL_IDS": "",
            "MERLIN_SAAS_TOKEN": "",
            "MERLIN_ACTIVITY_HOOKS": "off",
            "MERLIN_HOME": str(merlin_home),
            "CLAUDE_CONFIG_DIR": str(provider_home / "claude"),
            "CODEX_HOME": str(provider_home / "codex"),
        }
    )
    env.pop("MERLIN_TIMELINE_FIXTURES", None)
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    process = subprocess.Popen(
        ["uv", "run", "main.py", "--port", str(port)],
        cwd=root,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{port}"
    for _ in range(40):
        try:
            import urllib.request

            urllib.request.urlopen(f"{url}/timeline", timeout=1)
            break
        except Exception:
            time.sleep(0.25)
    else:
        process.kill()
        raise RuntimeError("Real Timeline test server failed to start")
    yield url
    process.send_signal(signal.SIGTERM)
    process.wait(timeout=5)


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as playwright:
        instance = playwright.chromium.launch(headless=True)
        yield instance
        instance.close()


def _page(browser, viewport):
    context = browser.new_context(viewport=viewport, device_scale_factor=1)
    page = context.new_page()
    errors = []
    page.on(
        "console",
        lambda message: (
            errors.append(message.text) if message.type == "error" else None
        ),
    )
    page.on("pageerror", lambda error: errors.append(str(error)))
    return context, page, errors


@pytest.mark.parametrize(
    "viewport",
    [
        {"width": 360, "height": 780},
        {"width": 768, "height": 820},
        {"width": 1440, "height": 900},
    ],
)
def test_populated_timeline_fits_each_composition(browser, timeline_server, viewport):
    context, page, errors = _page(browser, viewport)
    page.goto(f"{timeline_server}/timeline")
    page.locator(".timeline-item").first.wait_for()
    assert page.locator(".timeline-row").count() >= 6
    assert page.locator('[data-kind="tool.call"]').count() == 0
    assert page.locator("#timeline-anomaly").is_hidden()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    stage = page.locator("#timeline-stage").bounding_box()
    assert stage is not None
    assert stage["x"] >= 0
    assert stage["x"] + stage["width"] <= viewport["width"] + 1
    assert (
        page.locator(".timeline-track-label").first.evaluate(
            "el => getComputedStyle(el).position"
        )
        == "sticky"
    )
    assert errors == []
    context.close()


def test_mobile_selection_uses_bottom_sheet_and_keyboard_order(
    browser, timeline_server
):
    context, page, errors = _page(browser, {"width": 360, "height": 780})
    page.goto(f"{timeline_server}/timeline")
    item = page.locator('[data-id="turn-l2"]')
    item.wait_for()
    item.focus()
    item.press("ArrowRight")
    assert page.locator(".timeline-item:focus").count() == 1
    item.click()
    detail = page.locator("#timeline-detail")
    assert detail.get_attribute("aria-hidden") == "false"
    assert detail.bounding_box()["width"] == pytest.approx(360, abs=1)
    assert "selected=turn-l2" in page.url
    page.locator("#timeline-detail-close").click()
    assert detail.get_attribute("aria-hidden") == "true"
    assert errors == []
    context.close()


def test_capture_consent_explains_privacy_and_updates(browser, timeline_server):
    context, page, errors = _page(browser, {"width": 360, "height": 780})
    page.goto(f"{timeline_server}/timeline")
    panel = page.locator("#timeline-consent")
    panel.wait_for()
    assert "Never stores" in panel.inner_text()
    assert "tool inputs or results" in panel.inner_text()
    assert "otherwise follows the current tmux window" in panel.inner_text()
    page.locator('[data-capture-mode="off"]').click()
    page.locator("#timeline-consent-status").get_by_text(
        "Capture is off", exact=False
    ).wait_for()
    assert page.locator("#timeline-capture-mode").inner_text() == "off"
    assert errors == []
    context.close()


def test_real_store_disabled_state_survives_incremental_poll(
    browser, real_timeline_server
):
    context, page, errors = _page(browser, {"width": 360, "height": 780})
    page.goto(f"{real_timeline_server}/timeline")
    heading = page.locator("#timeline-state h2")
    heading.get_by_text("Activity history is off", exact=True).wait_for()
    page.wait_for_timeout(2200)
    assert heading.inner_text() == "Activity history is off"
    assert page.locator("#timeline-stage").is_hidden()
    assert errors == []
    context.close()


def test_grouping_controls_filter_and_url_state_restore(browser, timeline_server):
    context, page, errors = _page(browser, {"width": 1440, "height": 900})
    page.goto(
        f"{timeline_server}/timeline?group=activity&zoom=1.40&selected=review-wait"
    )
    page.locator(".timeline-item").first.wait_for()
    assert (
        page.locator('[data-track="activity-wait"] .timeline-track-name').inner_text()
        == "Waiting"
    )
    l2_box = page.locator('[data-id="turn-l2"]').bounding_box()
    docs_box = page.locator('[data-id="turn-claude"]').bounding_box()
    assert l2_box["y"] != docs_box["y"]
    assert page.locator("#timeline-detail").get_attribute("aria-hidden") == "false"
    page.locator("#timeline-detail-close").click()
    page.locator("#timeline-filter").click()
    assert page.locator(".timeline-item").count() == 2
    page.locator('[data-grouping="participants"]').click()
    assert "group=participants" in page.url
    assert page.locator('[data-track="agent-codex-l3"]').count() == 1
    assert errors == []
    context.close()


def test_custom_range_and_fit_use_the_requested_window_and_viewport(
    browser, timeline_server
):
    context, page, errors = _page(browser, {"width": 768, "height": 820})
    page.goto(f"{timeline_server}/timeline")
    page.locator(".timeline-item").first.wait_for()
    page.locator("#timeline-custom-hours").fill("10")
    with page.expect_request(
        lambda request: "/api/timeline?" in request.url and "range=600" in page.url,
        timeout=3000,
    ) as requested:
        page.locator("#timeline-custom-range button").click()
    query = parse_qs(urlparse(requested.value.url).query)
    start = datetime.fromisoformat(query["since"][0].replace("Z", "+00:00"))
    end = datetime.fromisoformat(query["until"][0].replace("Z", "+00:00"))
    assert end - start == timedelta(hours=10)
    assert "range=600" in page.url
    page.locator(".timeline-item").first.wait_for()

    page.locator("#timeline-fit").click()
    geometry = page.locator("#timeline-scroll").evaluate(
        "element => ({scrollWidth: element.scrollWidth, clientWidth: element.clientWidth, canvasWidth: element.firstElementChild.getBoundingClientRect().width})"
    )
    assert geometry["scrollWidth"] <= geometry["clientWidth"] + 1, geometry
    assert errors == []
    context.close()


def test_accessible_status_cues_open_spans_and_reduced_motion(browser, timeline_server):
    context = browser.new_context(
        viewport={"width": 768, "height": 820}, reduced_motion="reduce"
    )
    page = context.new_page()
    page.goto(f"{timeline_server}/timeline")
    page.locator(".timeline-item").first.wait_for()
    assert page.locator('[data-id="tool-failed"]').evaluate(
        "el => getComputedStyle(el).backgroundImage !== 'none'"
    )
    assert (
        page.locator('[data-id="turn-claude"]').evaluate(
            "el => getComputedStyle(el).borderStyle"
        )
        == "dashed"
    )
    assert page.locator('[data-id="turn-l3"]').bounding_box()["width"] > 100
    assert page.locator('[data-id="review-request"]').bounding_box()["width"] >= 30
    assert (
        page.locator(".timeline-item[aria-label]").count()
        == page.locator(".timeline-item").count()
    )
    span_label = page.locator('[data-id="turn-l3"]').get_attribute("aria-label")
    assert "Codex · Listen-L3" in span_label
    assert "Implement checkpoint L3" in span_label
    assert "running" in span_label
    assert "seconds" in span_label
    agent_styles = page.locator('.timeline-item[data-actor="agent"]').evaluate_all(
        """nodes => nodes.map(node => ({
            actor: node.dataset.actorId,
            slot: node.dataset.agentSlot,
            border: getComputedStyle(node).borderColor,
        }))"""
    )
    by_actor = {item["actor"]: (item["slot"], item["border"]) for item in agent_styles}
    assert len(by_actor) >= 3
    assert len(set(by_actor.values())) == len(by_actor)
    point = page.locator('[data-id="review-request"]')
    assert "Automation" in point.get_attribute("aria-label")
    assert "point event" in point.get_attribute("aria-label")
    assert (
        point.locator(".timeline-item-label").evaluate(
            "el => getComputedStyle(el).visibility"
        )
        == "hidden"
    )
    point.focus()
    assert (
        point.locator(".timeline-item-label").evaluate(
            "el => getComputedStyle(el).visibility"
        )
        == "visible"
    )
    page.locator('[data-id="turn-l3"]').click()
    assert (
        page.locator("#timeline-detail").evaluate(
            "el => getComputedStyle(el).transitionDuration"
        )
        == "0s"
    )
    context.close()


def test_two_hundred_percent_browser_zoom_reflows_without_page_overflow(
    browser, timeline_server
):
    context = browser.new_context(viewport={"width": 1440, "height": 900})
    page = context.new_page()
    session = context.new_cdp_session(page)
    session.send(
        "Emulation.setDeviceMetricsOverride",
        {
            "width": 720,
            "height": 450,
            "deviceScaleFactor": 2,
            "mobile": False,
        },
    )
    page.goto(f"{timeline_server}/timeline")
    page.locator(".timeline-item").first.wait_for()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    assert page.locator(".timeline-segmented").bounding_box()["width"] <= 688
    context.close()


@pytest.mark.parametrize(
    ("scenario", "heading"),
    [
        ("empty", "A quiet window"),
        ("disabled", "Activity history is off"),
        ("no-results", "No matching activity"),
        ("disconnected", "Timeline disconnected"),
        ("loading", "Loading activity"),
    ],
)
def test_mobile_non_populated_states_are_designed(
    browser, timeline_server, scenario, heading
):
    context, page, errors = _page(browser, {"width": 360, "height": 780})
    page.goto(f"{timeline_server}/timeline?state={scenario}")
    page.locator("#timeline-state h2").wait_for()
    assert page.locator("#timeline-state h2").inner_text() == heading
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    assert errors == []
    context.close()


def test_freeze_during_initial_load_is_safe(browser, timeline_server):
    context, page, errors = _page(browser, {"width": 360, "height": 780})
    page.add_init_script(
        """
        (() => {
            const nativeFetch = window.fetch.bind(window);
            window.fetch = (input, init) => {
                const url = typeof input === 'string' ? input : input.url;
                if (url.startsWith('/api/timeline?') && !window.timelineFirstReleased) {
                    return new Promise((resolve, reject) => {
                        window.releaseTimelineFirstFetch = () => {
                            window.timelineFirstReleased = true;
                            nativeFetch(input, init).then(resolve, reject);
                        };
                    });
                }
                return nativeFetch(input, init);
            };
        })();
        """
    )
    page.goto(f"{timeline_server}/timeline", wait_until="domcontentloaded")
    page.locator("#timeline-state h2").get_by_text("Loading activity").wait_for()
    page.locator("#timeline-live").click()
    assert page.locator("#timeline-live span").last.inner_text() == "Frozen"
    assert page.locator("#timeline-live").get_attribute("aria-pressed") == "false"
    page.evaluate("window.releaseTimelineFirstFetch()")
    page.locator(".timeline-item").first.wait_for()
    assert errors == []
    context.close()


def _live_item(item_id, started, *, phase="span", status="running", label="Turn"):
    return {
        "id": item_id,
        "phase": phase,
        "kind": "agent.turn" if phase == "span" else "human.answer",
        "trace_id": "trace-live",
        "span_id": "turn" if phase == "span" else None,
        "parent_id": None,
        "children": [],
        "actor": "agent" if phase == "span" else "human",
        "actor_id": "agent-a" if phase == "span" else "human",
        "actor_label": "Codex · Live" if phase == "span" else "Human",
        "role": "Implementer" if phase == "span" else None,
        "participant_track": "agent-a" if phase == "span" else "human",
        "activity_track": "activity-agent" if phase == "span" else "activity-human",
        "label": label,
        "context": {
            "provider": "Codex",
            "agent_sid": "agent-a",
            "project": "clover",
        },
        "attributes": {},
        "source": "fixture",
        "start_timestamp": started.isoformat().replace("+00:00", "Z"),
        "end_timestamp": None,
        "duration_ms": None,
        "status": status,
        "open": phase == "span" and status == "running",
        "anomaly": None,
        "start": 0,
        "end": None,
    }


def test_incremental_live_freeze_reconnect_and_selection(browser, timeline_server):
    context, page, errors = _page(browser, {"width": 768, "height": 820})
    base = datetime.now(timezone.utc)
    started = base - timedelta(seconds=20)
    first_point = _live_item(
        "event-answer-1",
        base + timedelta(milliseconds=500),
        phase="point",
        status="ok",
        label="Answer submitted",
    )
    second_point = _live_item(
        "event-answer-2",
        base + timedelta(seconds=1),
        phase="point",
        status="ok",
        label="Review completed",
    )
    calls = {"count": 0}

    def payload(items, updates, cursor):
        return {
            "state": "ready",
            "message": None,
            "source": "activity-store",
            "range": {
                "start": (base - timedelta(seconds=40))
                .isoformat()
                .replace("+00:00", "Z"),
                "end": (base + timedelta(seconds=20))
                .isoformat()
                .replace("+00:00", "Z"),
                "now": base.isoformat().replace("+00:00", "Z"),
                "seconds": 60,
            },
            "grouping": "participants",
            "tracks": {"participants": [], "activity": []},
            "lanes": [],
            "items": items,
            "updates": updates,
            "cursor": cursor,
            "partial": False,
            "anomalies": 0,
            "last_modified_ns": calls["count"],
        }

    def handler(route):
        calls["count"] += 1
        if calls["count"] == 1:
            route.fulfill(
                json=payload([_live_item("span:trace-live:turn", started)], [], "c1")
            )
        elif calls["count"] in {2, 3}:
            route.fulfill(
                json=payload(
                    [first_point],
                    [
                        {
                            "id": "span:trace-live:turn",
                            "end_timestamp": (base + timedelta(seconds=1))
                            .isoformat()
                            .replace("+00:00", "Z"),
                            "duration_ms": 21000,
                            "status": "ok",
                            "anomaly": None,
                        }
                    ],
                    f"c{calls['count']}",
                )
            )
        elif calls["count"] == 4:
            route.fulfill(status=200, content_type="application/json", body="{broken")
        else:
            route.fulfill(json=payload([second_point], [], f"c{calls['count']}"))

    page.route("**/api/timeline?*", handler)
    page.expose_function("timelineRequestCount", lambda: calls["count"])
    page.goto(f"{timeline_server}/timeline")
    turn = page.locator('[data-id="span:trace-live:turn"]')
    turn.wait_for()
    initial_width = turn.bounding_box()["width"]
    page.wait_for_timeout(1100)
    assert turn.bounding_box()["width"] > initial_width
    turn.click()
    page.locator("#timeline-live").evaluate("element => element.click()")
    frozen_width = turn.bounding_box()["width"]
    page.wait_for_function("() => window.timelineRequestCount().then(n => n >= 2)")
    page.wait_for_timeout(200)
    assert turn.bounding_box()["width"] == pytest.approx(frozen_width, abs=1)
    assert page.locator(".timeline-item").count() == 1

    page.locator("#timeline-live").evaluate("element => element.click()")
    page.locator('[data-id="event-answer-1"]').wait_for()
    assert page.locator("#timeline-detail").get_attribute("aria-hidden") == "false"
    assert page.locator("#timeline-detail-status").inner_text().lower() == "ok"
    page.wait_for_function("() => window.timelineRequestCount().then(n => n >= 3)")
    assert page.locator('[data-id="event-answer-1"]').count() == 1

    page.wait_for_function("() => window.timelineRequestCount().then(n => n >= 4)")
    page.locator("#timeline-connection").get_by_text(
        "Reconnecting", exact=False
    ).wait_for()
    page.wait_for_function("() => window.timelineRequestCount().then(n => n >= 5)")
    page.locator('[data-id="event-answer-2"]').wait_for()
    assert page.locator("#timeline-connection").is_hidden()
    assert page.locator(".timeline-item").count() == 3
    assert errors == []
    context.close()


def test_open_span_accessible_duration_advances_with_live_bar(browser, timeline_server):
    context, page, errors = _page(browser, {"width": 768, "height": 820})
    base = datetime.now(timezone.utc)
    item = _live_item("span:accessible:turn", base - timedelta(seconds=20))
    item["duration_ms"] = 1000
    calls = {"count": 0}

    def response(items, cursor):
        return {
            "state": "ready",
            "message": None,
            "source": "activity-store",
            "range": {
                "start": (base - timedelta(seconds=40))
                .isoformat()
                .replace("+00:00", "Z"),
                "end": (base + timedelta(seconds=20))
                .isoformat()
                .replace("+00:00", "Z"),
                "now": base.isoformat().replace("+00:00", "Z"),
                "seconds": 60,
            },
            "items": items,
            "updates": [],
            "cursor": cursor,
            "partial": False,
            "skipped": 0,
            "flagged": 0,
            "anomalies": 0,
            "dropped": 0,
        }

    def handler(route):
        calls["count"] += 1
        items = [] if "cursor=" in route.request.url else [item]
        route.fulfill(json=response(items, f"accessible-{calls['count']}"))

    page.route("**/api/timeline?*", handler)
    page.goto(f"{timeline_server}/timeline")
    turn = page.locator('[data-id="span:accessible:turn"]')
    turn.wait_for()
    turn.click()
    initial_label = turn.get_attribute("aria-label")
    initial_duration = int(
        page.locator('[data-field="duration"] dd').text_content().removesuffix("s")
    )

    page.wait_for_function(
        """initial => {
            const label = document.querySelector('[data-id="span:accessible:turn"]')
                .getAttribute('aria-label');
            return label !== initial;
        }""",
        arg=initial_label,
        timeout=3000,
    )

    assert (
        int(page.locator('[data-field="duration"] dd').text_content().removesuffix("s"))
        > initial_duration
    )
    assert errors == []
    context.close()


def test_open_span_is_rebaselined_when_actor_dies_and_duration_stays_unknown(
    browser, timeline_server
):
    context, page, errors = _page(browser, {"width": 768, "height": 820})
    page.add_init_script(
        """(() => {
            const nativeSetTimeout = window.setTimeout.bind(window);
            window.setTimeout = (callback, delay, ...args) =>
                nativeSetTimeout(callback, delay === 1500 ? 50 : delay, ...args);
        })();"""
    )
    base = datetime.now(timezone.utc)
    started = base - timedelta(seconds=20)
    calls = {"count": 0, "full": 0}

    def response(item, cursor):
        return {
            "state": "ready",
            "message": None,
            "source": "activity-store",
            "range": {
                "start": (base - timedelta(seconds=60))
                .isoformat()
                .replace("+00:00", "Z"),
                "end": (base + timedelta(seconds=60))
                .isoformat()
                .replace("+00:00", "Z"),
                "now": base.isoformat().replace("+00:00", "Z"),
                "seconds": 120,
            },
            "items": [item] if item else [],
            "updates": [],
            "cursor": cursor,
            "partial": False,
            "skipped": 0,
            "flagged": 0,
            "anomalies": 0,
            "dropped": 0,
        }

    def handler(route):
        calls["count"] += 1
        incremental = "cursor=" in route.request.url
        if incremental:
            route.fulfill(json=response(None, f"c{calls['count']}"))
            return

        calls["full"] += 1
        item = None
        if calls["full"] != 2:
            item = _live_item("span:dead:turn", started)
        if calls["full"] > 2:
            item.update(status="interrupted", open=False)
        route.fulfill(json=response(item, f"c{calls['count']}"))

    page.route("**/api/timeline?*", handler)
    page.expose_function("timelineFullCount", lambda: calls["full"])
    page.goto(f"{timeline_server}/timeline")
    turn = page.locator('[data-id="span:dead:turn"]')
    turn.wait_for()
    assert "running" in turn.get_attribute("aria-label")

    page.wait_for_function("() => window.timelineFullCount().then(n => n >= 2)")
    assert turn.count() == 1
    assert "running" in turn.get_attribute("aria-label")

    page.wait_for_function(
        "() => document.querySelector('[data-id=\"span:dead:turn\"]')"
        ".getAttribute('aria-label').includes('interrupted')",
        timeout=5000,
    )
    assert calls["full"] >= 2
    assert "duration unknown" in turn.get_attribute("aria-label")
    stopped_width = turn.bounding_box()["width"]
    page.wait_for_timeout(1100)
    assert turn.bounding_box()["width"] == pytest.approx(stopped_width, abs=1)

    turn.click()
    page.locator('#timeline-detail[aria-hidden="false"]').wait_for()
    assert page.locator("#timeline-detail-status").inner_text().lower() == "interrupted"
    detail = page.locator("#timeline-detail-grid").inner_text().lower()
    assert "no completion observed" in detail
    assert "duration\nunknown" in detail
    assert errors == []
    context.close()


def test_dense_response_caps_dom_and_keeps_interaction(browser, timeline_server):
    context, page, errors = _page(browser, {"width": 1440, "height": 900})
    base = datetime.now(timezone.utc)
    items = [
        _live_item(
            f"dense-{index}",
            base + timedelta(milliseconds=index),
            phase="point",
            status="ok",
            label=f"Point {index}",
        )
        for index in range(3000)
    ]

    def handler(route):
        route.fulfill(
            json={
                "state": "ready",
                "source": "deterministic-fixture",
                "range": {
                    "start": base.isoformat().replace("+00:00", "Z"),
                    "end": (base + timedelta(seconds=60))
                    .isoformat()
                    .replace("+00:00", "Z"),
                    "now": base.isoformat().replace("+00:00", "Z"),
                    "seconds": 60,
                },
                "items": items,
                "updates": [],
                "cursor": "dense",
                "partial": False,
                "anomalies": 0,
            }
        )

    page.route("**/api/timeline?*", handler)
    page.goto(f"{timeline_server}/timeline")
    page.locator(".timeline-item").first.wait_for()
    assert page.locator(".timeline-item").count() == 2500
    assert "Partial history" in page.locator("#timeline-connection").inner_text()
    page.locator(".timeline-item").first.click()
    assert page.locator("#timeline-detail").get_attribute("aria-hidden") == "false"
    page.locator(".timeline-item").first.focus()
    page.keyboard.press("ArrowRight")
    assert (
        page.locator(".timeline-item")
        .nth(1)
        .evaluate("element => element === document.activeElement")
    )

    page.evaluate(
        """() => {
            const oldItem = document.querySelector('.timeline-item');
            document.querySelector('[data-range="15"]').click();
            oldItem.dispatchEvent(new KeyboardEvent('keydown', {key: 'ArrowRight', bubbles: true}));
        }"""
    )
    assert errors == []
    context.close()


def test_minimap_tracks_horizontal_view(browser, timeline_server):
    context, page, errors = _page(browser, {"width": 768, "height": 820})
    page.goto(f"{timeline_server}/timeline")
    page.locator(".timeline-item").first.wait_for()
    for _ in range(7):
        page.locator("#timeline-zoom-in").click()
    minimap = page.locator("#timeline-minimap-window")
    initial_left = float(minimap.evaluate("element => parseFloat(element.style.left)"))
    page.locator("#timeline-scroll").evaluate(
        "element => { element.scrollLeft = element.scrollWidth; element.dispatchEvent(new Event('scroll')); }"
    )
    page.wait_for_timeout(50)
    final_left = float(minimap.evaluate("element => parseFloat(element.style.left)"))
    assert final_left > initial_left
    assert errors == []
    context.close()


def test_data_quality_and_capture_gap_have_truthful_copy(browser, timeline_server):
    context, page, errors = _page(browser, {"width": 768, "height": 820})
    base = datetime.now(timezone.utc)

    def handler(route):
        route.fulfill(
            json={
                "state": "ready",
                "source": "activity-store",
                "range": {
                    "start": base.isoformat().replace("+00:00", "Z"),
                    "end": (base + timedelta(seconds=60))
                    .isoformat()
                    .replace("+00:00", "Z"),
                    "now": base.isoformat().replace("+00:00", "Z"),
                    "seconds": 60,
                },
                "items": [_live_item("quality", base, phase="point", status="ok")],
                "updates": [],
                "cursor": "quality",
                "partial": False,
                "skipped": 1,
                "flagged": 2,
                "anomalies": 3,
                "dropped": 4,
            }
        )

    page.route("**/api/timeline?*", handler)
    page.goto(f"{timeline_server}/timeline")
    page.locator(".timeline-item").first.wait_for()
    quality = page.locator("#timeline-anomaly").inner_text()
    assert "1 records could not be read" in quality
    assert "2 incomplete lifecycles flagged" in quality
    assert (
        "Capture gap · 4 events were not written today"
        in page.locator("#timeline-connection").inner_text()
    )
    assert errors == []
    context.close()


def test_live_window_evicts_old_points_without_fabricating_duration(
    browser, timeline_server
):
    context, page, errors = _page(browser, {"width": 768, "height": 820})
    base = datetime.now(timezone.utc)
    point = _live_item(
        "aging-point",
        base - timedelta(milliseconds=500),
        phase="point",
        status="ok",
        label="Aging point",
    )
    calls = {"count": 0}

    def handler(route):
        calls["count"] += 1
        shifted = calls["count"] > 1
        start = (
            base + timedelta(milliseconds=500)
            if shifted
            else base - timedelta(seconds=60)
        )
        end = start + timedelta(seconds=60)
        route.fulfill(
            json={
                "state": "empty" if shifted else "ready",
                "message": "No activity in this range." if shifted else None,
                "source": "activity-store",
                "range": {
                    "start": start.isoformat().replace("+00:00", "Z"),
                    "end": end.isoformat().replace("+00:00", "Z"),
                    "now": end.isoformat().replace("+00:00", "Z"),
                    "seconds": 60,
                },
                "items": [point] if not shifted else [],
                "updates": [],
                "cursor": f"aging-{calls['count']}",
                "partial": False,
                "skipped": 0,
                "flagged": 0,
                "anomalies": 0,
                "dropped": 0,
            }
        )

    page.route("**/api/timeline?*", handler)
    page.goto(f"{timeline_server}/timeline")
    page.locator('[data-id="aging-point"]').wait_for()
    page.locator("#timeline-state h2").get_by_text("A quiet window").wait_for(
        timeout=4000
    )
    assert page.locator('[data-id="aging-point"]').count() == 0
    assert page.locator("#timeline-visible").inner_text() == "0 events"
    assert errors == []
    context.close()
