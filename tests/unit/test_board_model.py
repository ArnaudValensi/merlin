"""Tests for the Sessions board reconcile + view model (board/model.py).

Pure functions, no tmux: we hand-build Window records and a Store and assert the
reconcile (stop policy, identity pinning) and build_view (project grouping,
family nesting, stable order, attention) behavior.
"""

from board import model
from board.store import Session, Store
from board.sweep import Window


def win(
    sid="",
    state="idle",
    cwd="/home/u/proj",
    parent="",
    relation="",
    session="t",
    window_id="@1",
    active=False,
    activity=0,
    name="claude",
):
    return Window(
        sid=sid,
        state=state,
        cwd=cwd,
        parent=parent,
        relation=relation,
        session=session,
        window_id=window_id,
        active=active,
        activity=activity,
        name=name,
    )


def store_with(*recs):
    return Store(sessions={r.sid: r for r in recs})


# ---------------------------------------------------------------------------
# reconcile: upsert + identity pinning
# ---------------------------------------------------------------------------
class TestReconcileUpsert:
    def test_new_session_is_recorded_and_pinned(self):
        st = Store()
        model.reconcile(
            st,
            [
                win(
                    sid="s1",
                    state="busy",
                    cwd="/a/myproj",
                    parent="p1",
                    relation="child",
                )
            ],
            now=100.0,
        )
        rec = st.sessions["s1"]
        assert rec.first_seen == 100.0
        assert rec.last_seen == 100.0
        assert rec.state == "busy"
        assert rec.live is True
        assert rec.cwd == "/a/myproj"
        assert rec.project == "myproj"
        assert rec.parent == "p1"
        assert rec.relation == "child"

    def test_pinned_cwd_survives_a_later_different_sweep(self):
        st = store_with(
            Session(
                sid="s1", cwd="/a/launch", project="launch", first_seen=1.0, live=True
            )
        )
        # A later sweep reports a different cwd (should never happen since it's
        # pinned in tmux, but the model must not move it regardless).
        model.reconcile(st, [win(sid="s1", cwd="/a/elsewhere")], now=2.0)
        assert st.sessions["s1"].cwd == "/a/launch"
        assert st.sessions["s1"].project == "launch"

    def test_live_fields_refresh(self):
        st = store_with(Session(sid="s1", state="busy", live=True, first_seen=1.0))
        model.reconcile(
            st, [win(sid="s1", state="done", window_id="@9", session="work")], now=5.0
        )
        rec = st.sessions["s1"]
        assert rec.state == "done"
        assert rec.window_id == "@9"
        assert rec.session == "work"
        assert rec.last_seen == 5.0

    def test_windows_without_sid_are_ignored(self):
        st = Store()
        model.reconcile(st, [win(sid="", state="busy")], now=1.0)
        assert st.sessions == {}

    def test_plain_windows_are_ignored(self):
        st = Store()
        model.reconcile(st, [win(sid="s1", state="")], now=1.0)  # no @agent_state
        assert st.sessions == {}


# ---------------------------------------------------------------------------
# reconcile: the stop policy (vanish vs tombstone)
# ---------------------------------------------------------------------------
class TestStopPolicy:
    def test_idle_session_vanishes_on_disappearance(self):
        st = store_with(Session(sid="s1", state="idle", live=True, first_seen=1.0))
        model.reconcile(st, [], now=2.0)  # gone from sweep
        assert "s1" not in st.sessions

    def test_done_session_vanishes_on_disappearance(self):
        st = store_with(Session(sid="s1", state="done", live=True, first_seen=1.0))
        model.reconcile(st, [], now=2.0)
        assert "s1" not in st.sessions

    def test_busy_session_becomes_a_tombstone(self):
        st = store_with(Session(sid="s1", state="busy", live=True, first_seen=1.0))
        model.reconcile(st, [], now=2.0)
        rec = st.sessions["s1"]
        assert rec.live is False
        assert rec.tombstone is True
        assert rec.closed_at == 2.0

    def test_existing_tombstone_is_left_alone(self):
        st = store_with(
            Session(
                sid="s1",
                state="busy",
                live=False,
                tombstone=True,
                closed_at=1.0,
                first_seen=1.0,
            )
        )
        model.reconcile(st, [], now=9.0)
        assert st.sessions["s1"].tombstone is True
        assert st.sessions["s1"].closed_at == 1.0  # not re-stamped

    def test_reappearance_clears_tombstone(self):
        st = store_with(
            Session(
                sid="s1",
                state="busy",
                live=False,
                tombstone=True,
                closed_at=1.0,
                first_seen=1.0,
            )
        )
        model.reconcile(st, [win(sid="s1", state="busy")], now=3.0)
        assert st.sessions["s1"].live is True
        assert st.sessions["s1"].tombstone is False


# ---------------------------------------------------------------------------
# build_view: grouping, hierarchy, order, attention
# ---------------------------------------------------------------------------
class TestBuildView:
    def _view(self, store, windows, now=1.0):
        model.reconcile(store, windows, now)
        return model.build_view(store, windows, now)

    def test_groups_by_project(self):
        st = Store()
        v = self._view(
            st,
            [
                win(sid="a", cwd="/x/alpha"),
                win(sid="b", cwd="/x/alpha"),
                win(sid="c", cwd="/x/beta"),
            ],
        )
        names = {p["project"]: len(p["sessions"]) for p in v["projects"]}
        assert names == {"alpha": 2, "beta": 1}

    def test_child_nests_under_parent(self):
        st = Store()
        v = self._view(
            st,
            [
                win(sid="root", cwd="/x/alpha"),
                win(sid="kid", cwd="/x/alpha", parent="root", relation="child"),
            ],
        )
        proj = v["projects"][0]
        assert len(proj["sessions"]) == 1  # only the root at top level
        root = proj["sessions"][0]
        assert [c["sid"] for c in root["children"]] == ["kid"]
        assert root["children"][0]["depth"] == 1

    def test_sibling_stays_flat(self):
        # The fork/handoff default: a sibling is a peer, NOT nested under its
        # parent, even though it carries a parent link for the family record.
        st = Store()
        v = self._view(
            st,
            [
                win(sid="root", cwd="/x/alpha"),
                win(sid="twin", cwd="/x/alpha", parent="root", relation="sibling"),
            ],
        )
        proj = v["projects"][0]
        assert len(proj["sessions"]) == 2  # both at top level
        assert all(not s["children"] for s in proj["sessions"])

    def test_hierarchy_wins_over_project_placement(self):
        # A child launched in a DIFFERENT project still nests under its parent,
        # and does not spawn its own project bucket.
        st = Store()
        v = self._view(
            st,
            [
                win(sid="root", cwd="/x/alpha"),
                win(sid="kid", cwd="/y/other", parent="root", relation="child"),
            ],
        )
        assert [p["project"] for p in v["projects"]] == ["alpha"]
        root = v["projects"][0]["sessions"][0]
        assert root["children"][0]["sid"] == "kid"

    def test_orphan_child_becomes_root(self):
        # parent link points at a session that isn't shown -> child is a root.
        st = Store()
        v = self._view(
            st,
            [
                win(sid="kid", cwd="/x/alpha", parent="ghost", relation="child"),
            ],
        )
        assert v["projects"][0]["sessions"][0]["sid"] == "kid"

    def test_stable_order_not_by_state(self):
        # s1 idle with order 0, s2 done with order 1. A state-sorting board would
        # float the done one up; ours must keep 0,1 regardless of state.
        st = store_with(
            Session(
                sid="s1",
                cwd="/x/a",
                project="a",
                state="idle",
                live=True,
                first_seen=1.0,
                order=0.0,
            ),
            Session(
                sid="s2",
                cwd="/x/a",
                project="a",
                state="done",
                live=True,
                first_seen=2.0,
                order=1.0,
            ),
        )
        v = model.build_view(
            st,
            [
                win(sid="s1", state="idle", cwd="/x/a"),
                win(sid="s2", state="done", cwd="/x/a"),
            ],
            now=3.0,
        )
        order = [s["sid"] for s in v["projects"][0]["sessions"]]
        assert order == ["s1", "s2"]

    def test_manual_order_overrides_first_seen(self):
        st = store_with(
            Session(
                sid="early",
                cwd="/x/a",
                project="a",
                live=True,
                first_seen=1.0,
                order=5.0,
            ),
            Session(
                sid="late",
                cwd="/x/a",
                project="a",
                live=True,
                first_seen=9.0,
                order=1.0,
            ),
        )
        v = model.build_view(
            st, [win(sid="early", cwd="/x/a"), win(sid="late", cwd="/x/a")], now=10.0
        )
        assert [s["sid"] for s in v["projects"][0]["sessions"]] == ["late", "early"]

    def test_attention_counts_live_done_only(self):
        st = Store()
        v = self._view(
            st,
            [
                win(sid="a", state="done"),
                win(sid="b", state="done"),
                win(sid="c", state="busy"),
            ],
        )
        assert v["attention"] == 2

    def test_waiting_flag_and_active_flag(self):
        st = Store()
        v = self._view(st, [win(sid="a", state="done", active=True, window_id="@3")])
        node = v["projects"][0]["sessions"][0]
        assert node["waiting"] is True
        assert node["active"] is True

    def test_plain_windows_listed_separately(self):
        st = Store()
        v = self._view(
            st,
            [
                win(sid="a", state="busy"),
                win(sid="", state="", name="vim", session="t"),
            ],
        )
        assert len(v["other_windows"]) == 1
        assert v["other_windows"][0]["name"] == "vim"

    def test_tombstone_still_shown(self):
        st = store_with(
            Session(
                sid="s1",
                state="busy",
                cwd="/x/a",
                project="a",
                live=False,
                tombstone=True,
                first_seen=1.0,
            )
        )
        v = model.build_view(st, [], now=5.0)
        node = v["projects"][0]["sessions"][0]
        assert node["tombstone"] is True
