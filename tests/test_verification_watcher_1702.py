"""#1702 — the scheduled sweep that runs the checkers instead of waiting to be asked.

THREE CHECKERS SHIPPED IN ONE DAY AND NONE RAN ON ITS OWN. That is the status `measure.py`
had (#1681): cited as ENFORCING a rule while being an opt-in CLI with zero callers, for
months, precisely because it was available-when-needed.

MEASURED BEFORE BUILDING, and it moved the job off CI where the ticket first put it:
  · daemon binds 127.0.0.1, `daemon_url`/`tunnel_domain` empty, ZERO self-hosted runners
    -> a GitHub runner cannot reach the Queen, so "report to the Queen" is
    unimplementable there.
  · `gh api .../actions/secrets` -> total_count 0. FLEET_READ_TOKEN does not exist, and
    the last verify-citations CI run reported "PARTIAL coverage", `sources scanned: 1`
    of 13, arch target skipped.
  -> 0 of 3 checks reach full coverage in CI. Operator ruling: daemon-side.

THE TWO POLICIES UNDER TEST ARE THE WHOLE POINT:
  send on a finding, send NOTHING otherwise — or the channel gets muted and the one real
  finding arrives inside a stream of noise;
  a ZERO DENOMINATOR IS A FAILED RUN — a checker that parsed nothing has exonerated
  nothing, and reporting that as clean is worse than not running at all.
"""

from __future__ import annotations

import pytest

from swarm.drones.verification_watcher import (
    VerificationWatcher,
    looks_degraded,
    parse_denominator,
)

CHECKS = [("citations", ["verify-citations.py"], "citations")]


def _watcher(runner, notify=None, interval=86400.0):
    return VerificationWatcher(
        checks=CHECKS, run_check=runner, notify_queen=notify, interval_seconds=interval
    )


# ---------------------------------------------------------------------------
# Send on a finding, send nothing otherwise
# ---------------------------------------------------------------------------


def test_a_clean_run_sends_the_queen_nothing():
    """THE POLICY THAT KEEPS THE CHANNEL WORTH READING. A daily 'all clear' trains the
    reader to skip it, and then the one real finding arrives in a stream of noise."""
    sent: list[str] = []
    _watcher(lambda a: (0, "citations found : 12"), sent.append).sweep()

    assert sent == []


def test_a_finding_reaches_the_queen():
    sent: list[str] = []
    _watcher(lambda a: (1, "citations found : 12\nDANGLING x/y.md"), sent.append).sweep()

    assert len(sent) == 1
    assert "DANGLING" in sent[0]


def test_the_message_states_what_the_checks_do_not_verify():
    """#1681's lesson carried into the report: `measure.py` was present the whole time
    and 'it enforces the equivalent rule' was still false. Existence is not truth."""
    sent: list[str] = []
    _watcher(lambda a: (1, "citations found : 3\nDANGLING a"), sent.append).sweep()

    assert "do NOT verify" in sent[0] or "NOT verify" in sent[0]


# ---------------------------------------------------------------------------
# A zero denominator is a FAILED run, not a clean one
# ---------------------------------------------------------------------------


def test_a_zero_denominator_is_reported_even_though_the_checker_exited_clean():
    """THE LOAD-BEARING TEST. exit 0 with `citations found : 0` means the checker parsed
    NOTHING. verify-citations once reported 10 findings on a clean tree from an empty
    pathspec, and the line that exposed it was a denominator. Treating this as success is
    how a scheduled job becomes decorative."""
    sent: list[str] = []
    result = _watcher(lambda a: (0, "citations found : 0"), sent.append).sweep()

    assert result.outcomes[0].measured_nothing is True
    assert len(sent) == 1
    assert "MEASURED NOTHING" in sent[0]


def test_an_absent_denominator_is_also_a_failed_run():
    """Distinct from zero on purpose: 'the checker printed no denominator' means its
    output format changed underneath us, which conflating with 0 would hide."""
    sent: list[str] = []
    result = _watcher(lambda a: (0, "everything is fine!"), sent.append).sweep()

    assert result.outcomes[0].denominator is None
    assert len(sent) == 1


def test_a_healthy_denominator_with_a_clean_exit_stays_silent():
    """POSITIVE CONTROL for both rules above. A watcher that reported everything would
    pass every test so far and be muted within a week."""
    sent: list[str] = []
    result = _watcher(lambda a: (0, "citations found : 41"), sent.append).sweep()

    assert result.outcomes[0].measured_nothing is False
    assert sent == []


# ---------------------------------------------------------------------------
# Degraded coverage is visible, never silent
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "output",
    [
        "PARTIAL coverage — no FLEET_READ_TOKEN secret.\ncitations found : 5",
        "citations found : 5\nUNREADABLE SOURCE: origin/main:CLAUDE.md",
        "UNMEASURABLE   open-PR set\nbranches examined : 4",
    ],
)
def test_degraded_coverage_is_detected_from_the_checker_s_own_words(output):
    """AC4. The real CI run said 'PARTIAL coverage' and scanned 1 source of 13 — and it
    still showed a green tick, which is what got PR #1 merged. The checkers already
    announce this; the job's duty is not to swallow it."""
    assert looks_degraded(output) is True


def test_a_full_run_is_not_flagged_as_degraded():
    assert looks_degraded("citations found : 41\nDANGLING : 0") is False


def test_degraded_runs_do_not_count_toward_full_coverage():
    result = _watcher(lambda a: (0, "PARTIAL coverage\ncitations found : 5")).sweep()

    assert result.full_coverage == 0
    assert len(result.outcomes) == 1


# ---------------------------------------------------------------------------
# Denominator parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kind,output,want",
    [
        ("citations", "  citations found : 16", 16),
        ("citations", '{"citations_found": 7}', 7),
        ("containment", "branches examined : 13", 13),
        ("containment", '{"branches_examined": 0}', 0),
        ("citations", "no number here", None),
    ],
)
def test_denominators_are_read_from_text_or_json(kind, output, want):
    assert parse_denominator(kind, output) == want


# ---------------------------------------------------------------------------
# It must never be able to break the daemon
# ---------------------------------------------------------------------------


def test_a_checker_that_raises_is_a_finding_not_a_crash():
    """A checker that cannot run has NOT reported clean — the exact confusion this whole
    family of tickets exists to remove."""
    sent: list[str] = []

    def boom(argv):
        raise OSError("python3 not found")

    result = _watcher(boom, sent.append).sweep()

    assert result.outcomes[0].exit_code == -1
    assert len(sent) == 1


def test_a_notifier_that_raises_does_not_break_the_sweep():
    def boom(_msg):
        raise RuntimeError("message store down")

    _watcher(lambda a: (1, "citations found : 2\nDANGLING"), boom).sweep()  # must not raise


# ---------------------------------------------------------------------------
# Scheduling
# ---------------------------------------------------------------------------


def test_it_runs_on_the_first_tick_after_startup():
    """A fleet that restarts daily must still sweep. Waiting a full interval from boot
    means a frequently-restarted daemon never runs the check at all — available-when-
    needed again, in a new costume."""
    w = _watcher(lambda a: (0, "citations found : 1"))

    assert w.due(now=1.0) is True


def test_it_does_not_run_again_until_the_interval_elapses():
    w = _watcher(lambda a: (0, "citations found : 1"), interval=86400.0)
    w.sweep(now=1000.0)

    assert w.due(now=1000.0 + 3600) is False
    assert w.due(now=1000.0 + 86400) is True


def test_a_zero_interval_disables_it():
    assert _watcher(lambda a: (0, ""), interval=0).enabled is False


# ---------------------------------------------------------------------------
# The wiring — a policy nothing constructs is the defect this ticket is about
# ---------------------------------------------------------------------------


def test_the_pilot_constructs_the_watcher():
    """#1681's `measure.py` was a correct tool with ZERO CALLERS for months. A watcher
    that exists and is never built is the same artefact wearing a newer docstring."""
    import inspect

    from swarm.drones import pilot as pilot_mod

    src = inspect.getsource(pilot_mod)
    assert "VerificationWatcher(" in src
    assert "verification_interval_seconds" in src


def test_the_dispatcher_ticks_it():
    import inspect

    from swarm.drones import poll_dispatcher

    src = inspect.getsource(poll_dispatcher)
    assert "_run_verification_sweep" in src
    assert "self._run_verification_sweep," in src, "declared but never added to the sweep list"


def test_the_daemon_binds_the_queen_channel():
    import inspect

    from swarm.server import daemon as daemon_mod

    src = inspect.getsource(daemon_mod)
    assert "set_verification_notifier" in src
    assert "QUEEN_WORKER_NAME" in src


def test_the_config_key_survives_a_round_trip():
    """Four layers dropped `shortcuts` silently in #1677 while every unit test passed.
    A config key is not wired until it round-trips."""
    from swarm.config.models import HiveConfig
    from swarm.config.serialization import serialize_config

    cfg = HiveConfig()
    cfg.drones.verification_interval_seconds = 3600.0

    assert serialize_config(cfg)["drones"]["verification_interval_seconds"] == 3600.0


def test_the_default_checks_name_both_scripts():
    from swarm.drones.verification_watcher import default_verification_checks

    labels = [c[0] for c in default_verification_checks()]
    assert labels == ["citations", "containment"]


def test_a_missing_checker_raises_so_the_sweep_reports_it():
    """ "Did not run" and "found nothing" are different claims. The runner raises; the
    watcher turns that into a FINDING rather than a clean result."""
    import pytest as _pytest

    from swarm.drones.verification_watcher import run_check_subprocess

    with _pytest.raises(FileNotFoundError):
        run_check_subprocess(["python3", "/nonexistent/checker.py"])


def test_the_containment_denominator_is_derived_from_its_per_branch_verdicts():
    """CAUGHT BY RUNNING IT FOR REAL. The containment checker prints no total, so this
    returned None and the watcher labelled a GENUINE finding (one stale branch) as
    "BROKEN — MEASURED NOTHING". A checker that prints one line per branch has already
    said how many it examined; demanding it change format would have been wrong twice —
    it belongs to another worker, and the information was already there."""
    out = (
        "CONTAINED      origin/x  (28 added lines all present in origin/main)\n"
        "SKIPPED        origin/y  (open PR)\n"
        "UNMEASURABLE   origin/z  (no merge-base) — DO NOT DELETE\n"
        "\n1 stale branch(es) — every added line is already on origin/main."
    )

    assert parse_denominator("containment", out) == 3


def test_containment_with_no_branches_examined_is_still_measured_nothing():
    """The other direction, and the one that matters: no verdict lines means it really
    did examine nothing, and that must NOT be reported as clean."""
    assert parse_denominator("containment", "\n1 stale branch(es)\n") is None
