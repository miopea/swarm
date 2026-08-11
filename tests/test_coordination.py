"""Tests for file ownership + single-branch coordination (Phase 5)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from swarm.coordination.ownership import (
    FileOwnershipMap,
    OwnershipMode,
)
from swarm.coordination.sync import AutoPullSync, pull_worker
from swarm.worker.worker import Worker, WorkerState


def _make_worker(
    name: str = "w1",
    state: WorkerState = WorkerState.RESTING,
    has_process: bool = True,
    repo_path: str = "",
) -> Worker:
    w = Worker(name=name, path="/tmp/test")
    w.state = state
    w.repo_path = repo_path
    if has_process:
        proc = AsyncMock()
        proc.is_user_active = False
        w.process = proc
    return w


# --- OwnershipMode ---


class TestOwnershipMode:
    def test_values(self) -> None:
        assert OwnershipMode.OFF.value == "off"
        assert OwnershipMode.WARNING.value == "warning"
        assert OwnershipMode.HARD_BLOCK.value == "hard-block"


# --- FileOwnershipMap ---


class TestFileOwnershipMap:
    def test_claim_and_get_owner(self) -> None:
        m = FileOwnershipMap()
        m.claim("w1", {"src/api.py", "src/main.py"})
        assert m.get_owner("src/api.py") == "w1"
        assert m.get_owner("src/main.py") == "w1"
        assert m.get_owner("src/other.py") is None

    def test_claim_same_worker_no_overlap(self) -> None:
        m = FileOwnershipMap()
        overlaps = m.claim("w1", {"src/api.py"})
        assert overlaps == []
        # Same worker re-claiming — no overlap
        overlaps = m.claim("w1", {"src/api.py"})
        assert overlaps == []

    def test_claim_overlap_detected(self) -> None:
        m = FileOwnershipMap()
        m.claim("w1", {"src/api.py"})
        overlaps = m.claim("w2", {"src/api.py"})
        assert len(overlaps) == 1
        assert overlaps[0].file_path == "src/api.py"
        assert overlaps[0].owner == "w1"
        assert overlaps[0].intruder == "w2"

    def test_claim_multiple_files_partial_overlap(self) -> None:
        m = FileOwnershipMap()
        m.claim("w1", {"src/api.py"})
        overlaps = m.claim("w2", {"src/api.py", "src/new.py"})
        assert len(overlaps) == 1
        assert overlaps[0].file_path == "src/api.py"
        # new.py should be owned by w2
        assert m.get_owner("src/new.py") == "w2"

    def test_release(self) -> None:
        m = FileOwnershipMap()
        m.claim("w1", {"src/a.py", "src/b.py"})
        count = m.release("w1")
        assert count == 2
        assert m.get_owner("src/a.py") is None
        assert m.get_owner("src/b.py") is None

    def test_release_nonexistent_worker(self) -> None:
        m = FileOwnershipMap()
        count = m.release("w99")
        assert count == 0

    def test_release_file(self) -> None:
        m = FileOwnershipMap()
        m.claim("w1", {"src/api.py"})
        prev = m.release_file("src/api.py")
        assert prev == "w1"
        assert m.get_owner("src/api.py") is None

    def test_swarm_scaffolding_files_skipped(self, caplog) -> None:
        """Regression: ``.claude/commands/swarm-*`` and ``.claude/skills/swarm-*``
        files are Swarm's own scaffolding — installed identically into
        every worker repo by the hooks installer, never owned by any
        single worker.  Reported: every reload produced 50+ WARNING
        lines as each worker reported these as dirty and the ownership
        map flagged them as cross-worker overlaps.

        The fix: skip those paths entirely at the ownership layer.
        """
        import logging

        m = FileOwnershipMap()
        m.claim("worker_a", {".claude/commands/swarm-status.md"})
        with caplog.at_level(logging.WARNING, logger="swarm.coordination.ownership"):
            overlaps = m.claim(
                "worker_b",
                {
                    ".claude/commands/swarm-status.md",
                    ".claude/skills/swarm-checkpoint/SKILL.md",
                    ".claude/scheduled_tasks.lock",
                },
            )

        assert overlaps == [], "Scaffolding overlaps must not be reported"
        assert not any("file overlap" in r.getMessage() for r in caplog.records), (
            "Scaffolding files must not produce WARNING lines.  Got: "
            f"{[r.getMessage() for r in caplog.records]}"
        )

    def test_real_overlap_warning_is_coalesced(self, caplog) -> None:
        """Phase: when a worker overlaps multiple files owned by the
        same other worker, emit ONE WARNING listing them — not one
        WARNING per file.  Pre-fix a multi-file overlap dumped N
        nearly-identical lines into ``swarm.log``."""
        import logging

        m = FileOwnershipMap()
        m.claim("alice", {"src/a.py", "src/b.py", "src/c.py"})
        with caplog.at_level(logging.WARNING, logger="swarm.coordination.ownership"):
            overlaps = m.claim("bob", {"src/a.py", "src/b.py", "src/c.py"})

        assert len(overlaps) == 3  # underlying overlap records still per-file
        warnings = [r for r in caplog.records if "file overlap" in r.getMessage()]
        assert len(warnings) == 1, (
            "Expected one coalesced WARNING for the overlap, got "
            f"{len(warnings)}: {[r.getMessage() for r in warnings]}"
        )
        msg = warnings[0].getMessage()
        # The single line should mention all three files (or a truncated preview).
        assert "alice" in msg and "bob" in msg
        assert "3" in msg  # the count appears in the message

    def test_get_worker_files(self) -> None:
        m = FileOwnershipMap()
        m.claim("w1", {"src/a.py", "src/b.py"})
        files = m.get_worker_files("w1")
        assert files == {"src/a.py", "src/b.py"}
        # Returns a copy
        files.add("src/c.py")
        assert "src/c.py" not in m.get_worker_files("w1")

    def test_check_overlap_without_claiming(self) -> None:
        m = FileOwnershipMap()
        m.claim("w1", {"src/api.py"})
        overlaps = m.check_overlap("w2", {"src/api.py"})
        assert len(overlaps) == 1
        # Ownership should NOT have changed
        assert m.get_owner("src/api.py") == "w1"

    def test_update_from_conflicts(self) -> None:
        m = FileOwnershipMap()
        changed = {
            "w1": {"src/a.py", "src/b.py"},
            "w2": {"src/c.py"},
        }
        overlaps = m.update_from_conflicts(changed)
        assert overlaps == []
        assert m.get_owner("src/a.py") == "w1"
        assert m.get_owner("src/c.py") == "w2"

    def test_update_from_conflicts_with_overlap(self) -> None:
        m = FileOwnershipMap()
        m.claim("w1", {"src/shared.py"})
        changed = {"w2": {"src/shared.py", "src/new.py"}}
        overlaps = m.update_from_conflicts(changed)
        assert len(overlaps) == 1
        assert overlaps[0].file_path == "src/shared.py"

    def test_transfer(self) -> None:
        m = FileOwnershipMap()
        m.claim("w1", {"src/api.py"})
        old = m.transfer("src/api.py", "w2")
        assert old == "w1"
        assert m.get_owner("src/api.py") == "w2"
        assert "src/api.py" not in m.get_worker_files("w1")
        assert "src/api.py" in m.get_worker_files("w2")

    def test_to_dict(self) -> None:
        m = FileOwnershipMap()
        m.claim("w1", {"src/api.py"})
        d = m.to_dict()
        assert d["mode"] == "warning"
        assert d["files"] == {"src/api.py": "w1"}
        assert d["workers"] == {"w1": ["src/api.py"]}
        assert d["recent_overlaps"] == []

    def test_to_dict_with_overlaps(self) -> None:
        m = FileOwnershipMap()
        m.claim("w1", {"src/api.py"})
        m.claim("w2", {"src/api.py"})
        d = m.to_dict()
        assert len(d["recent_overlaps"]) == 1
        assert d["recent_overlaps"][0]["file"] == "src/api.py"

    def test_clear(self) -> None:
        m = FileOwnershipMap()
        m.claim("w1", {"src/api.py"})
        m.clear()
        assert m.get_owner("src/api.py") is None
        assert m.to_dict()["files"] == {}

    def test_mode_property(self) -> None:
        m = FileOwnershipMap(mode=OwnershipMode.HARD_BLOCK)
        assert m.mode == OwnershipMode.HARD_BLOCK
        m.mode = OwnershipMode.OFF
        assert m.mode == OwnershipMode.OFF

    def test_overlap_history_capped(self) -> None:
        m = FileOwnershipMap()
        m.claim("w1", {f"f{i}.py" for i in range(110)})
        for i in range(110):
            m.claim("w2", {f"f{i}.py"})
        # Should be capped at 100
        assert len(m._overlaps) <= 100


# --- AutoPullSync ---


class TestAutoPullSync:
    def test_disabled(self) -> None:
        sync = AutoPullSync(enabled=False)
        assert sync.enabled is False

    @pytest.mark.asyncio
    async def test_on_commit_pulls_idle_workers(self) -> None:
        sync = AutoPullSync(enabled=True)
        w1 = _make_worker("w1", WorkerState.BUZZING)
        w2 = _make_worker("w2", WorkerState.RESTING)
        w3 = _make_worker("w3", WorkerState.RESTING)

        pulled = await sync.on_commit("w1", [w1, w2, w3])
        assert pulled == 2
        w2.process.send_keys.assert_called_once()
        w3.process.send_keys.assert_called_once()

    @pytest.mark.asyncio
    async def test_on_commit_disabled(self) -> None:
        sync = AutoPullSync(enabled=False)
        w1 = _make_worker("w1")
        w2 = _make_worker("w2")
        pulled = await sync.on_commit("w1", [w1, w2])
        assert pulled == 0

    @pytest.mark.asyncio
    async def test_on_commit_skips_committer(self) -> None:
        sync = AutoPullSync(enabled=True)
        w1 = _make_worker("w1", WorkerState.RESTING)
        pulled = await sync.on_commit("w1", [w1])
        assert pulled == 0

    @pytest.mark.asyncio
    async def test_on_commit_skips_worktree_workers(self) -> None:
        sync = AutoPullSync(enabled=True)
        w1 = _make_worker("w1", WorkerState.BUZZING)
        w2 = _make_worker("w2", WorkerState.RESTING, repo_path="/repo")
        pulled = await sync.on_commit("w1", [w1, w2])
        assert pulled == 0

    @pytest.mark.asyncio
    async def test_on_commit_skips_buzzing(self) -> None:
        sync = AutoPullSync(enabled=True)
        w1 = _make_worker("w1", WorkerState.BUZZING)
        w2 = _make_worker("w2", WorkerState.BUZZING)
        pulled = await sync.on_commit("w1", [w1, w2])
        assert pulled == 0

    def test_get_status(self) -> None:
        sync = AutoPullSync(enabled=True)
        status = sync.get_status()
        assert status["enabled"] is True
        assert status["total_pulls"] == 0

    @pytest.mark.asyncio
    async def test_pull_history_tracked(self) -> None:
        sync = AutoPullSync(enabled=True)
        w1 = _make_worker("w1", WorkerState.BUZZING)
        w2 = _make_worker("w2", WorkerState.RESTING)
        await sync.on_commit("w1", [w1, w2])
        assert len(sync._pull_history) == 1
        assert sync._pull_history[0]["committer"] == "w1"
        assert sync._pull_history[0]["target"] == "w2"


# --- pull_worker ---


class TestPullWorker:
    @pytest.mark.asyncio
    async def test_pull_resting_worker(self) -> None:
        w = _make_worker("w1", WorkerState.RESTING)
        result = await pull_worker(w)
        assert result is True
        # automated=True is part of the contract, not incidental (#1451): a git
        # pull is a scheduled housekeeping write, so it must be held rather than
        # typed into an open AskUserQuestion. Asserted explicitly so that
        # dropping the flag fails here instead of silently re-opening the hole.
        w.process.send_keys.assert_called_once_with("git pull --rebase --quiet\n", automated=True)

    @pytest.mark.asyncio
    async def test_pull_sleeping_worker(self) -> None:
        w = _make_worker("w1", WorkerState.SLEEPING)
        result = await pull_worker(w)
        assert result is True

    @pytest.mark.asyncio
    async def test_skip_buzzing_worker(self) -> None:
        w = _make_worker("w1", WorkerState.BUZZING)
        result = await pull_worker(w)
        assert result is False

    @pytest.mark.asyncio
    async def test_skip_no_process(self) -> None:
        w = _make_worker("w1", has_process=False)
        result = await pull_worker(w)
        assert result is False

    @pytest.mark.asyncio
    async def test_skip_user_active(self) -> None:
        w = _make_worker("w1", WorkerState.RESTING)
        w.process.is_user_active = True
        result = await pull_worker(w)
        assert result is False


# --- Config Integration ---


class TestConfigIntegration:
    def test_coordination_config_defaults(self) -> None:
        from swarm.config import CoordinationConfig

        cfg = CoordinationConfig()
        assert cfg.mode == "single-branch"
        assert cfg.auto_pull is True
        assert cfg.file_ownership == "warning"

    def test_hive_config_has_coordination(self) -> None:
        from swarm.config import HiveConfig

        cfg = HiveConfig()
        assert cfg.coordination.mode == "single-branch"

    def test_config_validation_valid(self) -> None:
        from swarm.config import HiveConfig

        cfg = HiveConfig()
        errors = cfg.validate()
        coord_errors = [e for e in errors if "coordination" in e]
        assert coord_errors == []

    def test_config_validation_invalid_mode(self) -> None:
        from swarm.config import CoordinationConfig, HiveConfig

        cfg = HiveConfig(coordination=CoordinationConfig(mode="invalid"))
        errors = cfg.validate()
        assert any("coordination.mode" in e for e in errors)

    def test_config_validation_invalid_ownership(self) -> None:
        from swarm.config import CoordinationConfig, HiveConfig

        cfg = HiveConfig(coordination=CoordinationConfig(file_ownership="x"))
        errors = cfg.validate()
        assert any("coordination.file_ownership" in e for e in errors)

    def test_config_serialization(self) -> None:
        from swarm.config import HiveConfig, serialize_config

        cfg = HiveConfig()
        data = serialize_config(cfg)
        assert "coordination" in data
        assert data["coordination"]["mode"] == "single-branch"
        assert data["coordination"]["auto_pull"] is True
        assert data["coordination"]["file_ownership"] == "warning"
