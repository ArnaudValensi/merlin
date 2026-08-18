"""Tests for the session switcher view model (board/model.py). Pure functions,
no tmux: feed Window/TmuxSession records to build_tree and assert the tree."""

from board import model
from board.sweep import TmuxSession, Window


def win(session="alpha", wid="@1", index=0, state="", name="claude", **over):
    base = dict(
        sid="",
        state=state,
        cwd="",
        parent="",
        relation="",
        session=session,
        window_id=wid,
        index=index,
        active=False,
        activity=0,
        name=name,
    )
    base.update(over)
    return Window(**base)


def sess(name="alpha", attached=True, windows=1):
    return TmuxSession(
        name=name,
        session_id="$" + name,
        created=1,
        attached=attached,
        windows=windows,
        activity=0,
    )


class TestBuildTree:
    def test_groups_windows_under_their_session(self):
        sessions = [sess("alpha"), sess("beta")]
        windows = [
            win(session="alpha", wid="@1", index=1),
            win(session="alpha", wid="@2", index=2),
            win(session="beta", wid="@3", index=1),
        ]
        tree = model.build_tree(sessions, windows, "alpha", 0.0)
        by_name = {s["name"]: s for s in tree["sessions"]}
        assert [w["window_id"] for w in by_name["alpha"]["windows"]] == ["@1", "@2"]
        assert [w["window_id"] for w in by_name["beta"]["windows"]] == ["@3"]

    def test_shows_plain_windows_not_only_agents(self):
        # The reversal of the old board: a window with no @agent_state still shows.
        windows = [
            win(wid="@1", index=1, state="busy"),
            win(wid="@2", index=2, state="", name="shell"),
        ]
        tree = model.build_tree([sess("alpha")], windows, "alpha", 0.0)
        nodes = tree["sessions"][0]["windows"]
        assert len(nodes) == 2
        plain = next(n for n in nodes if n["window_id"] == "@2")
        assert plain["is_agent"] is False
        assert plain["state"] == ""

    def test_windows_ordered_by_index(self):
        windows = [
            win(wid="@a", index=3),
            win(wid="@b", index=1),
            win(wid="@c", index=2),
        ]
        tree = model.build_tree([sess("alpha")], windows, "alpha", 0.0)
        assert [w["window_id"] for w in tree["sessions"][0]["windows"]] == [
            "@b",
            "@c",
            "@a",
        ]

    def test_child_window_nests_under_parent(self):
        parent = win(wid="@1", index=1, sid="p", state="idle")
        child = win(
            wid="@2", index=2, sid="c", parent="p", relation="child", state="idle"
        )
        tree = model.build_tree([sess("alpha")], [parent, child], "alpha", 0.0)
        nodes = tree["sessions"][0]["windows"]
        assert nodes[0]["window_id"] == "@1" and nodes[0]["depth"] == 0
        assert nodes[1]["window_id"] == "@2" and nodes[1]["depth"] == 1

    def test_sibling_windows_stay_flat(self):
        a = win(wid="@1", index=1, sid="a", relation="sibling", state="idle")
        b = win(
            wid="@2", index=2, sid="b", parent="a", relation="sibling", state="idle"
        )
        tree = model.build_tree([sess("alpha")], [a, b], "alpha", 0.0)
        nodes = tree["sessions"][0]["windows"]
        assert all(n["depth"] == 0 for n in nodes)

    def test_per_session_counts(self):
        windows = [
            win(wid="@1", index=1, state="busy"),
            win(wid="@2", index=2, state="done"),
            win(wid="@3", index=3, state=""),
            win(wid="@4", index=4, state="ask"),
        ]
        tree = model.build_tree([sess("alpha")], windows, "alpha", 0.0)
        counts = tree["sessions"][0]["counts"]
        assert counts == {"total": 4, "working": 1, "waiting": 1, "asking": 1}

    def test_attention_totals_across_all_sessions(self):
        sessions = [sess("alpha"), sess("beta")]
        windows = [
            win(session="alpha", wid="@1", index=1, state="done"),
            win(session="beta", wid="@2", index=1, state="done"),
            win(session="beta", wid="@3", index=2, state="busy"),
        ]
        tree = model.build_tree(sessions, windows, "alpha", 0.0)
        assert tree["attention"] == 2  # two waiting, across both sessions
        assert tree["counts"] == {
            "sessions": 2,
            "waiting": 2,
            "working": 1,
            "asking": 0,
        }

    def test_asking_counts_toward_attention(self):
        """A window blocked on a question wants you just as much as a finished
        one, so the badge must include it, from any session."""
        sessions = [sess("alpha"), sess("beta")]
        windows = [
            win(session="alpha", wid="@1", index=1, state="ask"),
            win(session="beta", wid="@2", index=1, state="done"),
            win(session="beta", wid="@3", index=2, state="ask"),
            win(session="beta", wid="@4", index=3, state="busy"),
        ]
        tree = model.build_tree(sessions, windows, "alpha", 0.0)
        assert tree["attention"] == 3  # two asking + one waiting
        assert tree["counts"] == {
            "sessions": 2,
            "waiting": 1,
            "working": 1,
            "asking": 2,
        }

    def test_asking_is_kept_apart_from_waiting_and_busy(self):
        """'ask' is its own state: it is neither unread-finished nor working, so
        a UI can rank it above 'done' rather than merging the two."""
        windows = [
            win(wid="@1", index=1, state="ask"),
            win(wid="@2", index=2, state="done"),
            win(wid="@3", index=3, state="busy"),
        ]
        tree = model.build_tree([sess("alpha")], windows, "alpha", 0.0)
        nodes = {n["window_id"]: n for n in tree["sessions"][0]["windows"]}
        assert nodes["@1"]["asking"] is True
        assert nodes["@1"]["waiting"] is False
        assert nodes["@1"]["busy"] is False
        assert nodes["@2"]["asking"] is False
        assert nodes["@2"]["waiting"] is True
        assert nodes["@3"]["asking"] is False
        assert nodes["@3"]["busy"] is True

    def test_current_session_flag(self):
        tree = model.build_tree([sess("alpha"), sess("beta")], [], "beta", 0.0)
        flags = {s["name"]: s["current"] for s in tree["sessions"]}
        assert flags == {"alpha": False, "beta": True}
        assert tree["current_session"] == "beta"

    def test_session_order_follows_tmux_not_activity(self):
        # Input order is preserved (tmux order), never re-sorted by state/activity.
        sessions = [sess("zeta"), sess("alpha")]
        tree = model.build_tree(sessions, [], "", 0.0)
        assert [s["name"] for s in tree["sessions"]] == ["zeta", "alpha"]

    def test_active_window_marked(self):
        windows = [win(wid="@1", index=1, active=True), win(wid="@2", index=2)]
        tree = model.build_tree([sess("alpha")], windows, "alpha", 0.0)
        nodes = {n["window_id"]: n for n in tree["sessions"][0]["windows"]}
        assert nodes["@1"]["active"] is True
        assert nodes["@2"]["active"] is False

    def test_project_derived_from_cwd(self):
        windows = [win(wid="@1", index=1, cwd="/home/u/my-proj")]
        tree = model.build_tree([sess("alpha")], windows, "alpha", 0.0)
        assert tree["sessions"][0]["windows"][0]["project"] == "my-proj"

    def test_empty_session_has_no_windows(self):
        tree = model.build_tree([sess("alpha", windows=0)], [], "alpha", 0.0)
        assert tree["sessions"][0]["windows"] == []
        assert tree["sessions"][0]["counts"] == {
            "total": 0,
            "working": 0,
            "waiting": 0,
            "asking": 0,
        }
