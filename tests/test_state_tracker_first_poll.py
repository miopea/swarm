"""Diagnostic for #1357: what the first classification after a restart decides.

Persisting worker state (2026.8.9.21) did NOT stop every worker showing BUZZING after a
reload. The restore demonstrably runs — self.workers starts empty so every worker takes
that branch — and the saved map was two minutes old, well inside the staleness window.
Yet the fleet came up all-BUZZING and the store faithfully recorded that.

The leading hypothesis is that re-attaching to a PTY replays a SNAPSHOT of recent
output, and an activity indicator inside that replay is classified as live work — which
would mean the restore can never win: it sets the truth and the next poll overwrites it
from stale buffer content.

I was already wrong once on this bug by reasoning from code rather than measuring, so
this logs the evidence to settle it instead of shipping a second guess.
"""

from __future__ import annotations

import logging

from swarm.worker.worker import Worker, WorkerState


def _tracker() -> object:
    from swarm.drones.state_tracker import WorkerStateTracker

    t = WorkerStateTracker.__new__(WorkerStateTracker)
    t._first_classification_logged = set()
    return t


def _worker(name: str = "api", state: WorkerState = WorkerState.RESTING) -> Worker:
    w = Worker(name=name, path="/tmp")
    w.state = state
    return w


def test_it_records_what_the_state_was_and_what_was_decided(caplog):
    """Both halves matter: "was RESTING, decided BUZZING" is the finding, and either
    alone proves nothing."""
    t = _tracker()
    with caplog.at_level(logging.WARNING):
        t._log_first_classification(_worker(), WorkerState.BUZZING, "esc to interrupt")

    msg = " ".join(r.getMessage() for r in caplog.records)
    assert "was=RESTING" in msg and "decided=BUZZING" in msg
    assert "api" in msg


def test_it_includes_the_content_it_decided_from(caplog):
    """Without the content there is no way to tell WHICH signal matched, which is the
    whole question."""
    t = _tracker()
    with caplog.at_level(logging.WARNING):
        t._log_first_classification(_worker(), WorkerState.BUZZING, "x" * 50 + "esc to interrupt")

    msg = " ".join(r.getMessage() for r in caplog.records)
    assert "esc to interrupt" in msg
    assert "content=66ch" in msg


def test_it_logs_at_WARNING_so_an_operator_sees_it(caplog):
    """A diagnostic at INFO is invisible at the default level — the same mistake the
    worklog success line made."""
    t = _tracker()
    with caplog.at_level(logging.WARNING):
        t._log_first_classification(_worker(), WorkerState.BUZZING, "c")
    assert any(r.levelno >= logging.WARNING for r in caplog.records)


def test_it_fires_ONCE_per_worker(caplog):
    """Bounded to sixteen lines once per daemon start, not a stream on every poll."""
    t = _tracker()
    w = _worker()
    with caplog.at_level(logging.WARNING):
        for _ in range(5):
            t._log_first_classification(w, WorkerState.BUZZING, "c")
    assert len([r for r in caplog.records if "#1357" in r.getMessage()]) == 1


def test_each_worker_gets_its_own_line(caplog):
    t = _tracker()
    with caplog.at_level(logging.WARNING):
        t._log_first_classification(_worker("api"), WorkerState.BUZZING, "c")
        t._log_first_classification(_worker("web"), WorkerState.RESTING, "c")
    msgs = [r.getMessage() for r in caplog.records if "#1357" in r.getMessage()]
    assert len(msgs) == 2


def test_a_huge_buffer_is_truncated(caplog):
    """A PTY snapshot can be 100KB; the log line must stay readable."""
    t = _tracker()
    with caplog.at_level(logging.WARNING):
        t._log_first_classification(_worker(), WorkerState.BUZZING, "y" * 100_000)
    line = next(r.getMessage() for r in caplog.records if "#1357" in r.getMessage())
    assert len(line) < 400, f"the diagnostic line is {len(line)} chars"
    assert "content=100000ch" in line, "the true size should still be reported"
