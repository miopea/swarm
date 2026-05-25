"""Tests for :mod:`swarm.queen.runtime` — Queen CLAUDE.md reconcile + spawn.

Pure-function focus: the reconcile logic + sync CLI entry are well-
contained and filesystem-bound, so they unit-test cleanly via
``tmp_path``. The PTY spawn path (``ensure_queen_running``) is left
to integration coverage in ``test_queen.py`` / ``test_fresh_install_queen.py``;
the spawn requires a real pool + worker manager which is more
integration than unit.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from swarm.queen.runtime import (
    CLAUDE_MD_FILENAME,
    DRIFT_SHIPPED_LAST_SUFFIX,
    DRIFT_SHIPPED_LATEST_SUFFIX,
    SHIPPED_MARKER_FILENAME,
    ClaudeMdReconcileResult,
    ReconcileAction,
    _ensure_queen_claude_md,
    find_queen,
    queen_worker_config,
    reconcile_queen_claude_md,
    sync_queen_claude_md,
)

# ---------------------------------------------------------------------------
# ClaudeMdReconcileResult value object
# ---------------------------------------------------------------------------


class TestClaudeMdReconcileResult:
    def test_equality_by_action_and_details(self) -> None:
        a = ClaudeMdReconcileResult("seeded", "x")
        b = ClaudeMdReconcileResult("seeded", "x")
        c = ClaudeMdReconcileResult("seeded", "y")
        d = ClaudeMdReconcileResult("no-op", "x")
        assert a == b
        assert a != c
        assert a != d

    def test_equality_returns_notimplemented_for_other_types(self) -> None:
        a = ClaudeMdReconcileResult("seeded")
        # Equality with non-result returns False (via NotImplemented).
        assert (a == "seeded") is False
        assert (a == 42) is False

    def test_repr_includes_action_and_details(self) -> None:
        r = ClaudeMdReconcileResult("auto-updated", "shipped changed")
        text = repr(r)
        assert "auto-updated" in text
        assert "shipped changed" in text

    def test_default_details_is_empty(self) -> None:
        r = ClaudeMdReconcileResult("no-op")
        assert r.details == ""


# ---------------------------------------------------------------------------
# reconcile_queen_claude_md — the 5-state decision matrix
# ---------------------------------------------------------------------------


class TestReconcileFreshSeed:
    def test_creates_workdir_target_and_marker(self, tmp_path: Path) -> None:
        workdir = tmp_path / "new"
        result = reconcile_queen_claude_md(workdir, shipped_latest="FIRST")
        assert result.action == ReconcileAction.SEEDED
        target = workdir / CLAUDE_MD_FILENAME
        marker = workdir / SHIPPED_MARKER_FILENAME
        assert target.read_text() == "FIRST"
        assert marker.read_text() == "FIRST"

    def test_workdir_auto_created(self, tmp_path: Path) -> None:
        workdir = tmp_path / "does" / "not" / "exist"
        assert not workdir.exists()
        reconcile_queen_claude_md(workdir, shipped_latest="x")
        assert workdir.is_dir()

    def test_details_carries_target_path(self, tmp_path: Path) -> None:
        result = reconcile_queen_claude_md(tmp_path, shipped_latest="x")
        assert str(tmp_path / CLAUDE_MD_FILENAME) in result.details


class TestReconcileMarkerSeed:
    def test_existing_file_no_marker_seeds_marker_from_disk(self, tmp_path: Path) -> None:
        """Operator upgraded from a swarm version that lacked the marker."""
        (tmp_path / CLAUDE_MD_FILENAME).write_text("ON_DISK_CONTENT")
        # marker does not exist
        result = reconcile_queen_claude_md(tmp_path, shipped_latest="LATEST")
        assert result.action == ReconcileAction.MARKER_SEEDED
        marker = (tmp_path / SHIPPED_MARKER_FILENAME).read_text()
        # Marker must mirror on-disk, not shipped — the on-disk content
        # is the baseline since we have no historical reference.
        assert marker == "ON_DISK_CONTENT"
        # On-disk untouched
        assert (tmp_path / CLAUDE_MD_FILENAME).read_text() == "ON_DISK_CONTENT"


class TestReconcileNoOp:
    def test_shipped_unchanged_is_no_op(self, tmp_path: Path) -> None:
        # Bootstrap so target + marker exist with matching shipped.
        reconcile_queen_claude_md(tmp_path, shipped_latest="SAME")
        result = reconcile_queen_claude_md(tmp_path, shipped_latest="SAME")
        assert result.action == ReconcileAction.NO_OP


class TestReconcileAutoUpdate:
    def test_shipped_changed_no_local_edits_replaces(self, tmp_path: Path) -> None:
        # Bootstrap with shipped=v1, then mutate "shipped" to v2 with
        # on-disk still matching the v1 marker (no operator edits).
        reconcile_queen_claude_md(tmp_path, shipped_latest="v1")
        result = reconcile_queen_claude_md(tmp_path, shipped_latest="v2")
        assert result.action == ReconcileAction.AUTO_UPDATED
        assert (tmp_path / CLAUDE_MD_FILENAME).read_text() == "v2"
        assert (tmp_path / SHIPPED_MARKER_FILENAME).read_text() == "v2"


class TestReconcileDrift:
    def test_shipped_changed_with_local_edits_writes_diff_refs(self, tmp_path: Path) -> None:
        # Bootstrap, then operator edits on-disk AND shipped changes.
        reconcile_queen_claude_md(tmp_path, shipped_latest="v1")
        (tmp_path / CLAUDE_MD_FILENAME).write_text("v1 + my edits")
        result = reconcile_queen_claude_md(tmp_path, shipped_latest="v2")
        assert result.action == ReconcileAction.DRIFT_FLAGGED
        # On-disk preserved
        assert (tmp_path / CLAUDE_MD_FILENAME).read_text() == "v1 + my edits"
        # Diff refs written
        latest = tmp_path / f"{CLAUDE_MD_FILENAME}{DRIFT_SHIPPED_LATEST_SUFFIX}"
        last = tmp_path / f"{CLAUDE_MD_FILENAME}{DRIFT_SHIPPED_LAST_SUFFIX}"
        assert latest.read_text() == "v2"
        assert last.read_text() == "v1"
        # Marker NOT updated — the operator hasn't reconciled yet
        assert (tmp_path / SHIPPED_MARKER_FILENAME).read_text() == "v1"

    def test_drift_details_names_both_ref_files(self, tmp_path: Path) -> None:
        reconcile_queen_claude_md(tmp_path, shipped_latest="v1")
        (tmp_path / CLAUDE_MD_FILENAME).write_text("v1 + edits")
        result = reconcile_queen_claude_md(tmp_path, shipped_latest="v2")
        assert f"{CLAUDE_MD_FILENAME}{DRIFT_SHIPPED_LATEST_SUFFIX}" in result.details
        assert f"{CLAUDE_MD_FILENAME}{DRIFT_SHIPPED_LAST_SUFFIX}" in result.details


class TestEnsureQueenClaudeMd:
    def test_is_backward_compat_alias_for_reconcile(self, tmp_path: Path) -> None:
        # The wrapper exists to keep older call sites working — assert
        # it returns the same shape.
        result = _ensure_queen_claude_md(tmp_path)
        # Fresh tmp_path → SEEDED outcome from the underlying reconcile
        assert isinstance(result, ClaudeMdReconcileResult)
        assert result.action == ReconcileAction.SEEDED


# ---------------------------------------------------------------------------
# sync_queen_claude_md — operator CLI entry
# ---------------------------------------------------------------------------


class TestSyncQueenClaudeMd:
    def test_accept_shipped_replaces_on_disk(self, tmp_path: Path) -> None:
        # Bootstrap drift scenario
        reconcile_queen_claude_md(tmp_path, shipped_latest="v1")
        (tmp_path / CLAUDE_MD_FILENAME).write_text("v1 + edits")
        reconcile_queen_claude_md(tmp_path, shipped_latest="v2")  # creates drift refs
        latest = tmp_path / f"{CLAUDE_MD_FILENAME}{DRIFT_SHIPPED_LATEST_SUFFIX}"
        last = tmp_path / f"{CLAUDE_MD_FILENAME}{DRIFT_SHIPPED_LAST_SUFFIX}"
        assert latest.exists() and last.exists()

        result = sync_queen_claude_md("accept-shipped", workdir=tmp_path)

        assert result.action == ReconcileAction.AUTO_UPDATED
        # On-disk replaced with the live QUEEN_SYSTEM_PROMPT
        from swarm.queen.runtime import QUEEN_SYSTEM_PROMPT

        assert (tmp_path / CLAUDE_MD_FILENAME).read_text() == QUEEN_SYSTEM_PROMPT
        assert (tmp_path / SHIPPED_MARKER_FILENAME).read_text() == QUEEN_SYSTEM_PROMPT
        # Drift artifacts removed
        assert not latest.exists()
        assert not last.exists()

    def test_keep_local_updates_marker_only(self, tmp_path: Path) -> None:
        reconcile_queen_claude_md(tmp_path, shipped_latest="v1")
        (tmp_path / CLAUDE_MD_FILENAME).write_text("my local v1.5")
        reconcile_queen_claude_md(tmp_path, shipped_latest="v2")
        latest = tmp_path / f"{CLAUDE_MD_FILENAME}{DRIFT_SHIPPED_LATEST_SUFFIX}"
        last = tmp_path / f"{CLAUDE_MD_FILENAME}{DRIFT_SHIPPED_LAST_SUFFIX}"
        assert latest.exists() and last.exists()

        result = sync_queen_claude_md("keep-local", workdir=tmp_path)

        assert result.action == ReconcileAction.NO_OP
        # On-disk preserved exactly
        assert (tmp_path / CLAUDE_MD_FILENAME).read_text() == "my local v1.5"
        # Marker updated to shipped
        from swarm.queen.runtime import QUEEN_SYSTEM_PROMPT

        assert (tmp_path / SHIPPED_MARKER_FILENAME).read_text() == QUEEN_SYSTEM_PROMPT
        # Drift artifacts cleared
        assert not latest.exists()
        assert not last.exists()

    def test_unknown_mode_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="unknown sync mode"):
            sync_queen_claude_md("not-a-mode", workdir=tmp_path)

    def test_accept_shipped_creates_workdir_if_missing(self, tmp_path: Path) -> None:
        workdir = tmp_path / "fresh"
        result = sync_queen_claude_md("accept-shipped", workdir=workdir)
        assert result.action == ReconcileAction.AUTO_UPDATED
        assert (workdir / CLAUDE_MD_FILENAME).exists()


# ---------------------------------------------------------------------------
# queen_worker_config — the synthetic Queen WorkerConfig
# ---------------------------------------------------------------------------


class TestQueenWorkerConfig:
    def test_uses_QUEEN_WORKER_NAME_and_work_dir(self) -> None:
        from swarm.queen.runtime import QUEEN_WORK_DIR
        from swarm.worker.worker import QUEEN_WORKER_NAME

        cfg = MagicMock()
        cfg.provider = "claude"
        wc = queen_worker_config(cfg)
        assert wc.name == QUEEN_WORKER_NAME
        assert wc.path == str(QUEEN_WORK_DIR)
        assert wc.identity == "queen"
        assert wc.provider == "claude"

    def test_falls_back_to_claude_when_provider_none(self) -> None:
        cfg = MagicMock()
        cfg.provider = None
        wc = queen_worker_config(cfg)
        assert wc.provider == "claude"


# ---------------------------------------------------------------------------
# find_queen
# ---------------------------------------------------------------------------


class TestFindQueen:
    def test_returns_none_when_no_queen(self) -> None:
        from swarm.worker.worker import Worker

        workers = [Worker(name="alpha", path="/tmp/a"), Worker(name="beta", path="/tmp/b")]
        # Defaults to is_queen=False on plain Worker construction.
        assert find_queen(workers) is None

    def test_returns_queen_when_present(self) -> None:
        from swarm.worker.worker import WORKER_KIND_QUEEN, Worker

        workers = [Worker(name="alpha", path="/tmp/a")]
        queen = Worker(name="queen", path="/tmp/q", kind=WORKER_KIND_QUEEN)
        workers.append(queen)
        assert find_queen(workers) is queen

    def test_returns_first_queen_when_multiple(self) -> None:
        """Shouldn't happen in production but the function returns the
        first match — pin the deterministic behaviour."""
        from swarm.worker.worker import WORKER_KIND_QUEEN, Worker

        q1 = Worker(name="queen", path="/tmp/q1", kind=WORKER_KIND_QUEEN)
        q2 = Worker(name="queen2", path="/tmp/q2", kind=WORKER_KIND_QUEEN)
        assert find_queen([q1, q2]) is q1
