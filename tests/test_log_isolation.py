"""The test suite must never write to the operator's production log (#1285).

FOUND 2026-08-06 while establishing, for #1275's AC-4, whether the running daemon
had reloaded onto current code. ``~/.swarm/swarm.log`` was the ONLY usable evidence
for that — the PID and start time are useless because Reload uses ``os.execv``,
which preserves both — and the log turned out to contain 3400 lines of test output:
``/tmp/pytest-of-*`` socket paths and ``unittest/mock.py`` tracebacks interleaved
with real daemon entries.

WHY THE EXISTING GUARD WAS NOT ENOUGH. ``conftest._isolate_logging`` already patched
``setup_logging`` in two namespaces and cleared the ``swarm`` logger — ONCE, at
session start. The leaked lines were INFO level, which a WARNING logger cannot emit,
so something re-configured logging MID-SESSION, after that one-time clear. Running
the tests that emitted them in isolation is completely clean, which is exactly why
this survived: it only appears when an earlier test in the same session re-attaches a
real file handler, so no single test file reproduces it.

THE FIX IS BEHAVIOURAL RATHER THAN A LIST OF KNOWN CALL SITES: a per-test autouse
fixture strips any handler pointing at the production log, before and after every
test, and names the test that attached one. It does not need to know who did it,
which is the property the old guard lacked.

MEASURED, and measured by ATTRIBUTION rather than file size: a full 5738-test run
added 0 lines matching ``pytest-of``/``unittest/mock.py``, down from 3400
accumulated. Size is the wrong instrument here — the live daemon appends to the same
file continuously, so the file grows during any test run whether or not the suite
wrote to it. An earlier size-based check showed +328 bytes that were entirely the
daemon's own term-trace.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

_PROD_LOG = Path.home() / ".swarm" / "swarm.log"


def _prod_handlers() -> list[logging.Handler]:
    out = []
    for name in ("swarm", ""):
        for h in logging.getLogger(name).handlers:
            target = getattr(h, "baseFilename", None)
            if target and os.path.abspath(target) == os.path.abspath(str(_PROD_LOG)):
                out.append(h)
    return out


def test_no_production_log_handler_is_attached_during_a_test():
    """The steady state every test should observe."""
    assert not _prod_handlers(), (
        "a handler on the production log is attached while tests run — "
        f"{[getattr(h, 'baseFilename', h) for h in _prod_handlers()]}"
    )


def test_the_guard_strips_a_handler_attached_mid_test():
    """THE CONTROL ON THE FIX. Without this, "no pollution appeared" is equally
    consistent with the guard working and with nothing having tried to pollute — and
    those two look identical in the log.

    Attaches a real handler on the production log, exactly as a mid-session
    ``setup_logging`` would, and asserts the guard removes it. The emit is
    deliberately NOT performed: proving the strip works must not itself write to the
    operator's log.
    """
    from tests.conftest import _strip_prod_log_handlers

    logger = logging.getLogger("swarm")
    handler = logging.FileHandler(str(_PROD_LOG), delay=True)  # delay: opens on emit
    logger.addHandler(handler)
    try:
        assert _prod_handlers(), "fixture failed to attach — the control proves nothing"

        found = _strip_prod_log_handlers()

        assert found is True, "the guard did not report finding a production handler"
        assert not _prod_handlers(), "the guard left a production-log handler attached"
    finally:
        # Belt and braces if the guard regressed; removeHandler is idempotent.
        logger.removeHandler(handler)


def test_the_guard_is_quiet_when_nothing_is_attached():
    """It must not claim to have found something on every call, or the
    session-finish report would name every test in the suite as a culprit."""
    from tests.conftest import _strip_prod_log_handlers

    assert not _prod_handlers(), "precondition: something is already attached"
    assert _strip_prod_log_handlers() is False


def test_the_default_log_path_is_redirected():
    """Second layer. Patching ``setup_logging`` only helps when the call resolves
    through that module attribute; anything reaching the real function with
    ``log_file=None`` would fall back to the default. So the default is redirected
    too, because this failure mode is silent."""
    import swarm.logging as _swarm_logging

    assert _swarm_logging._DEFAULT_LOG_FILE != "~/.swarm/swarm.log", (
        "the default log path is not redirected during tests"
    )


@pytest.mark.skipif(not _PROD_LOG.exists(), reason="no production log on this machine")
def test_this_test_session_is_not_appending_to_the_production_log():
    """End-to-end, at the level the operator cares about: emit at every level
    through the swarm logger and assert the production log did not grow.

    Compares the file's SIZE across a same-instant emit, which is safe here in a way
    a whole-session size comparison is not: the daemon's own writes are ~30s apart,
    so a synchronous emit-and-check cannot be confounded by them.
    """
    before = _PROD_LOG.stat().st_size

    log = logging.getLogger("swarm.test_log_isolation")
    log.debug("debug from the test suite")
    log.info("info from the test suite — this is the level that leaked")
    log.warning("warning from the test suite")
    log.error("error from the test suite")
    for h in logging.getLogger("swarm").handlers:
        h.flush()

    assert _PROD_LOG.stat().st_size == before, (
        "the test suite wrote to the operator's production log"
    )
