"""Unit tests for the Attention exception-queue classifier.

``attention_model.classify`` is a pure function, so these tests need no
daemon, DB, or fixtures — just snapshot dataclasses and a fixed clock.
They pin the policy: what gets surfaced, at what severity, what is
suppressed into the "Queen is handling" drawer, and the age-escalation
that promotes stale decisions to critical.
"""

from __future__ import annotations

from swarm.server import attention_model as am

NOW = 1_000_000.0


def _thread(kind: str, *, age: float = 60.0, latest: str | None = None, **kw) -> am.ThreadSnap:
    return am.ThreadSnap(
        id=kw.get("id", "t1"),
        kind=kind,
        title=kw.get("title", f"{kind} thing"),
        worker_name=kw.get("worker_name", "hub"),
        task_id=kw.get("task_id"),
        created_at=NOW - age,
        updated_at=NOW - age,
        latest_message=latest,
    )


def _proposal(*, age: float, **kw) -> am.ProposalSnap:
    return am.ProposalSnap(
        id=kw.get("id", "p1"),
        proposal_type=kw.get("proposal_type", "assignment"),
        worker_name=kw.get("worker_name", "hub"),
        task_id=kw.get("task_id", "task-1"),
        task_title=kw.get("task_title", "Do the thing"),
        reasoning=kw.get("reasoning", "because reasons"),
        assessment=kw.get("assessment", ""),
        confidence=kw.get("confidence", 0.62),
        is_plan=kw.get("is_plan", False),
        created_at=NOW - age,
    )


def _worker(name: str, state: str, *, dur: float = 60.0, **kw) -> am.WorkerSnap:
    return am.WorkerSnap(
        name=name,
        state=state,
        state_duration=dur,
        needs_operator_input=kw.get("needs_operator_input", state == "WAITING"),
        in_revive_grace=kw.get("in_revive_grace", False),
        task_id=kw.get("task_id"),
        waiting_excerpt=kw.get("waiting_excerpt"),
        revive_count=kw.get("revive_count", 0),
        last_stung_detail=kw.get("last_stung_detail"),
    )


def _run(**kw) -> am.AttentionView:
    base = dict(
        threads=[],
        proposals=[],
        workers=[],
        nudged_workers=set(),
        blocked_workers=set(),
        resource_snapshot=None,
        now=NOW,
    )
    base.update(kw)
    return am.classify(**base)


# ---------------------------------------------------------------------------
# Threads
# ---------------------------------------------------------------------------


def test_fresh_worker_message_idle_queen_is_handled_awaiting():
    # Recent message, Queen idle → still in flight (queued for her turn).
    view = _run(threads=[_thread("worker-message", age=60.0)])
    assert view.critical == [] and view.decision == []
    assert len(view.handled) == 1
    assert view.handled[0].reason == "relayed — awaiting her next turn"


def test_stale_worker_message_idle_queen_is_dropped_entirely():
    # Old message + idle Queen → she already dealt with it (threads
    # aren't auto-resolved). Don't imply she's still working — drop it.
    view = _run(threads=[_thread("worker-message", age=1200.0)])
    assert view.critical == [] and view.decision == [] and view.handled == []


def test_stale_worker_message_busy_queen_is_handled_now():
    # Old thread but the Queen is actively processing → plausibly on it.
    view = _run(threads=[_thread("worker-message", age=1200.0)], queen_busy=True)
    assert len(view.handled) == 1
    assert view.handled[0].reason == "with the Queen now"


def test_operator_thread_is_excluded_entirely():
    view = _run(threads=[_thread("operator")])
    assert view.critical == [] and view.decision == [] and view.handled == []


def test_queen_escalation_thread_is_a_decision_with_detail_and_actions():
    view = _run(threads=[_thread("queen-escalation", latest="worker stuck on auth refactor")])
    assert len(view.decision) == 1
    item = view.decision[0]
    assert item.kind == "queen-escalation"
    assert item.severity == am.SEVERITY_DECISION
    assert "auth refactor" in item.detail
    assert item.actions == ["reply", "focus", "dismiss"]


def test_queen_escalation_without_message_gets_fallback_detail():
    view = _run(threads=[_thread("oversight", latest=None)])
    assert view.decision[0].detail == "Queen flagged this for your review."


# ---------------------------------------------------------------------------
# Proposals
# ---------------------------------------------------------------------------


def test_young_proposal_is_handled_drones_still_evaluating():
    view = _run(proposals=[_proposal(age=30.0)])
    assert view.decision == []
    assert len(view.handled) == 1
    assert "drones evaluating" in view.handled[0].reason


def test_proposal_past_autonomous_window_is_a_decision_with_approve_reject():
    view = _run(proposals=[_proposal(age=300.0, reasoning="needs human judgment")])
    assert len(view.decision) == 1
    item = view.decision[0]
    assert item.kind == "proposal"
    assert item.ref_id == "p1"
    assert item.actions == ["approve", "reject", "focus"]
    assert "conf 0.62" in item.detail
    assert "needs human judgment" in item.detail


# ---------------------------------------------------------------------------
# Workers — STUNG
# ---------------------------------------------------------------------------


def test_stung_within_revive_grace_is_handled():
    view = _run(workers=[_worker("hub", "STUNG", in_revive_grace=True)])
    assert view.critical == []
    assert len(view.handled) == 1
    assert view.handled[0].reason == "reviving…"


def test_stung_past_grace_is_critical_needs_revive():
    view = _run(workers=[_worker("hub", "STUNG", revive_count=1)])
    assert len(view.critical) == 1
    item = view.critical[0]
    assert item.kind == "worker-stung"
    assert item.severity == am.SEVERITY_CRITICAL
    assert "needs revive" in item.title
    assert item.actions == ["revive", "focus"]


def test_stung_crash_loop_when_revive_count_exceeds_min():
    view = _run(workers=[_worker("hub", "STUNG", revive_count=4, dur=540.0)])
    item = view.critical[0]
    assert "crash loop" in item.title
    assert "revived 4×" in item.title


def test_stung_last_detail_surfaces_in_card():
    view = _run(workers=[_worker("hub", "STUNG", last_stung_detail="OOM killed")])
    assert "OOM killed" in view.critical[0].detail


# ---------------------------------------------------------------------------
# Workers — WAITING
# ---------------------------------------------------------------------------


def test_waiting_worker_blocked_is_handled():
    view = _run(
        workers=[_worker("hub", "WAITING")],
        blocked_workers={"hub"},
    )
    assert view.decision == []
    assert view.handled[0].reason == "blocked by a reported dependency"


def test_waiting_worker_being_nudged_is_handled():
    view = _run(
        workers=[_worker("hub", "WAITING")],
        nudged_workers={"hub"},
    )
    assert view.decision == []
    assert view.handled[0].reason == "Queen/idle-watcher nudging"


def test_waiting_worker_unhandled_is_a_decision():
    view = _run(workers=[_worker("hub", "WAITING", waiting_excerpt="Pick an option [1/2]")])
    assert len(view.decision) == 1
    item = view.decision[0]
    assert item.kind == "worker-waiting"
    assert item.actions == ["focus", "force_rest"]
    assert "Pick an option" in item.detail


def test_waiting_worker_below_grace_is_excluded_entirely():
    # needs_operator_input is False until the 15s grace passes — such a
    # transient prompt is noise, not even drawer-worthy.
    w = _worker("hub", "WAITING", needs_operator_input=False)
    view = _run(workers=[w])
    assert view.critical == [] and view.decision == [] and view.handled == []


def test_resting_worker_is_never_surfaced():
    view = _run(workers=[_worker("hub", "RESTING", needs_operator_input=False)])
    assert view.critical == [] and view.decision == [] and view.handled == []


# ---------------------------------------------------------------------------
# Resource pressure
# ---------------------------------------------------------------------------


def test_critical_memory_pressure_is_a_critical_item():
    snap = {"pressure_level": "critical", "mem_percent": 97, "swap_percent": 88}
    view = _run(resource_snapshot=snap)
    assert len(view.critical) == 1
    assert view.critical[0].kind == "resource"
    assert "97%" in view.critical[0].detail


def test_dstate_pids_produce_a_critical_item():
    view = _run(resource_snapshot={"pressure_level": "nominal", "dstate_pids": {123: "python"}})
    crit = [i for i in view.critical if i.id == "resource:dstate"]
    assert len(crit) == 1
    assert "uninterruptible sleep" in crit[0].title


def test_nominal_pressure_surfaces_nothing():
    view = _run(resource_snapshot={"pressure_level": "nominal", "mem_percent": 40})
    assert view.critical == []


# ---------------------------------------------------------------------------
# Age-escalation + sorting
# ---------------------------------------------------------------------------


def test_stale_decision_promotes_to_critical():
    cfg = am.AttentionConfig(stale_promote_seconds=1800.0)
    view = _run(
        proposals=[_proposal(age=3600.0)],  # 1h old, well past 30m
        cfg=cfg,
    )
    assert view.decision == []
    assert len(view.critical) == 1
    item = view.critical[0]
    assert item.severity == am.SEVERITY_CRITICAL
    assert item.detail.startswith("STALE")


def test_decision_just_under_threshold_stays_decision():
    cfg = am.AttentionConfig(stale_promote_seconds=1800.0)
    view = _run(proposals=[_proposal(age=1700.0)], cfg=cfg)
    assert len(view.decision) == 1
    assert view.critical == []


def test_within_tier_sorted_oldest_first():
    view = _run(
        threads=[
            _thread("queen-escalation", id="new", age=60.0),
            _thread("queen-escalation", id="old", age=600.0),
        ]
    )
    assert [i.ref_id for i in view.decision] == ["old", "new"]


def test_to_dict_shape_matches_frontend_contract():
    view = _run(
        threads=[_thread("queen-escalation"), _thread("worker-message")],
        workers=[_worker("hub", "STUNG")],
    )
    d = view.to_dict()
    assert set(d.keys()) == {"critical", "decision", "handled"}
    assert d["handled"]["count"] == 1
    assert isinstance(d["handled"]["items"], list)
    assert d["critical"][0]["kind"] == "worker-stung"
    assert d["decision"][0]["kind"] == "queen-escalation"
    # ExceptionItem dict carries everything the card renderer needs.
    assert set(d["decision"][0].keys()) >= {
        "id",
        "ref_id",
        "kind",
        "severity",
        "title",
        "detail",
        "worker_name",
        "actions",
        "options",
    }


# ---------------------------------------------------------------------------
# Choice-prompt options on a worker-waiting card (answer inline)
# ---------------------------------------------------------------------------

_MENU = "Do you want to proceed?\n❯ 1. Yes\n  2. Yes, and don't ask again\n  3. No, keep working\n"


def test_extract_choice_options_parses_numbered_menu():
    opts = am.extract_choice_options(_MENU)
    assert opts == [
        {"value": "1", "label": "Yes"},
        {"value": "2", "label": "Yes, and don't ask again"},
        {"value": "3", "label": "No, keep working"},
    ]


def test_extract_choice_options_empty_for_freeform_text():
    prose = "No — and to be precise about state: nothing is committed or pushed yet."
    assert am.extract_choice_options(prose) == []
    assert am.extract_choice_options(None) == []


def test_extract_choice_options_requires_cursor_and_other():
    # A plain numbered list with no focused (>/❯) option is not a live
    # menu — mirrors provider.has_choice_prompt; don't sprout fake buttons.
    assert am.extract_choice_options("1. one\n2. two\n") == []


def test_waiting_card_surfaces_worker_choice_options():
    w = _worker("realtruth", "WAITING", waiting_excerpt=_MENU)
    view = _run(workers=[w])
    assert len(view.decision) == 1
    item = view.decision[0]
    assert item.kind == "worker-waiting"
    assert [o["value"] for o in item.options] == ["1", "2", "3"]
    assert item.options[0]["label"] == "Yes"
    # Generic verbs stay as the fallback alongside the inline answers.
    assert item.actions == ["focus", "force_rest"]
    assert view.to_dict()["decision"][0]["options"][0] == {"value": "1", "label": "Yes"}


def test_waiting_card_no_options_when_not_a_menu():
    w = _worker("realtruth", "WAITING", waiting_excerpt="What should I name the table?")
    view = _run(workers=[w])
    assert view.decision[0].options == []
