"""Relocation driven from the dashboard instead of a terminal.

The command already existed; what was missing was any way to reach it
without a shell, which made "update in the UI" a half-truth — the last
step still dropped the operator to a prompt.

The properties worth pinning are the ones that make an unattended
relocation survivable: it must never run in-process (``relocate()``
SIGTERMs the daemon PID, which is us), it must never launch itself under
the ``swarm`` name it is about to delete, and it must refuse outright
when systemd would kill it partway through rather than discovering that
after the state has moved.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from aiohttp.test_utils import make_mocked_request

from swarm import relocate as rl
from swarm.server.routes import system


def _plan(
    *,
    tmp_path: Path,
    move_needed: bool = True,
    old_unit: Path | None = None,
    entrypoints: list[Path] | None = None,
    live: list[rl.LiveProcess] | None = None,
) -> rl.RelocationPlan:
    return rl.RelocationPlan(
        source=tmp_path / ".swarm",
        target=tmp_path / ".swarm-legacy",
        move_needed=move_needed,
        old_unit=old_unit,
        new_unit=tmp_path / "swarm-legacy.service",
        unit_active=True,
        old_entrypoints=entrypoints or [],
        live=live or [],
    )


@pytest.fixture
def spawns(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    """Record every subprocess the handler tries to launch."""
    calls: list[dict[str, object]] = []

    async def _fake_exec(*args: str, **kwargs: object) -> MagicMock:
        calls.append({"argv": list(args), "kwargs": kwargs})
        return MagicMock()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
    return calls


@pytest.fixture
def helper_ok(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    helper = tmp_path / "swarm-legacy"
    helper.write_text("#!/bin/sh\n")
    monkeypatch.setattr(system, "_relocate_helper", lambda: helper)
    monkeypatch.setattr(system, "_kill_mode_guard", lambda: None)
    return helper


def _post() -> object:
    return make_mocked_request("POST", "/api/relocate")


class TestRefusals:
    def test_already_relocated_does_not_spawn_anything(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, spawns: list, helper_ok: Path
    ) -> None:
        done = _plan(tmp_path=tmp_path, move_needed=False)
        assert done.already_done, "positive control — this plan is a no-op"
        monkeypatch.setattr(rl, "plan", lambda: done)

        resp = asyncio.run(system.handle_relocate(_post()))

        assert resp.status == 200
        assert b'"already"' in resp.body
        assert spawns == [], "a finished relocation must not re-run the helper"

    def test_kill_mode_guard_refuses_before_touching_state(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, spawns: list
    ) -> None:
        """Without KillMode=process the stop kills the helper mid-move.

        Discovering that afterwards means state moved and no unit written
        — the one outcome that needs a terminal to repair.
        """
        monkeypatch.setattr(rl, "plan", lambda: _plan(tmp_path=tmp_path))
        monkeypatch.setattr(system, "_relocate_helper", lambda: tmp_path / "swarm-legacy")
        monkeypatch.setattr(system, "_kill_mode_guard", lambda: "unit would kill the helper")

        resp = asyncio.run(system.handle_relocate(_post()))

        assert resp.status == 409
        assert spawns == [], "refused, so nothing may have been launched"

    def test_missing_helper_is_an_error_not_a_silent_noop(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, spawns: list
    ) -> None:
        monkeypatch.setattr(rl, "plan", lambda: _plan(tmp_path=tmp_path))
        monkeypatch.setattr(system, "_relocate_helper", lambda: None)
        monkeypatch.setattr(system, "_kill_mode_guard", lambda: None)

        resp = asyncio.run(system.handle_relocate(_post()))

        assert resp.status == 500
        assert spawns == []


class TestHandoff:
    def test_helper_runs_detached_so_the_stop_cannot_kill_it(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, spawns: list, helper_ok: Path
    ) -> None:
        monkeypatch.setattr(rl, "plan", lambda: _plan(tmp_path=tmp_path))

        resp = asyncio.run(system.handle_relocate(_post()))

        assert resp.status == 200
        assert len(spawns) == 1, "exactly one helper"
        assert spawns[0]["argv"] == [str(helper_ok), "relocate", "--yes"]
        assert spawns[0]["kwargs"]["start_new_session"] is True, (
            "without a new session the helper dies with the daemon it stops"
        )

    def test_helper_is_never_the_shim_the_relocation_deletes(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """``_remove_old_entrypoints()`` unlinks ``swarm`` partway through.

        A helper launched under that name would delete its own
        executable while running, so the resolver must only ever return
        ``swarm-legacy``.
        """
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        (bin_dir / "swarm").write_text("#!/bin/sh\n")
        (bin_dir / "swarm-legacy").write_text("#!/bin/sh\n")
        monkeypatch.setattr(sys, "executable", str(bin_dir / "python"))

        found = system._relocate_helper()

        assert found is not None
        assert found.name == "swarm-legacy"


class TestKillModeGuard:
    def test_absent_unit_does_not_block(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Nothing stops a unit that does not exist, so nothing kills the helper."""
        monkeypatch.setattr("swarm.service.current_unit_path", lambda: tmp_path / "absent.service")
        assert system._kill_mode_guard() is None

    def test_kill_mode_process_passes(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        unit = tmp_path / "swarm.service"
        unit.write_text("[Service]\nKillMode=process\nExecStart=/x\n")
        monkeypatch.setattr("swarm.service.current_unit_path", lambda: unit)
        assert system._kill_mode_guard() is None

    def test_kill_mode_mixed_is_refused_with_a_fix(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        unit = tmp_path / "swarm.service"
        unit.write_text("[Service]\nKillMode=mixed\nExecStart=/x\n")
        monkeypatch.setattr("swarm.service.current_unit_path", lambda: unit)

        message = system._kill_mode_guard()

        assert message is not None
        assert "Reload swarm once" in message, "a refusal must name the way out"


class TestPayload:
    def test_reports_what_the_operator_is_about_to_lose(self, tmp_path: Path) -> None:
        source = tmp_path / ".swarm"
        source.mkdir()
        (source / "swarm.db").write_bytes(b"x" * 2048)
        plan_ = rl.RelocationPlan(
            source=source,
            target=tmp_path / ".swarm-legacy",
            move_needed=True,
            old_unit=tmp_path / "swarm.service",
            new_unit=tmp_path / "swarm-legacy.service",
            unit_active=True,
            live=[rl.LiveProcess("pty-holder", 679, "owns every worker terminal")],
        )

        payload = system._relocation_payload(plan_)

        assert payload["relocated"] is False
        assert payload["size_bytes"] == 2048
        assert payload["old_unit"] == "swarm.service"
        assert payload["live"] == [
            {"kind": "pty-holder", "pid": 679, "detail": "owns every worker terminal"}
        ]

    def test_unwalkable_source_reports_unknown_not_zero(self, tmp_path: Path) -> None:
        """ "0 B" would read as "nothing to move" and invite skipping it."""
        assert rl.dir_size_bytes(tmp_path / "does-not-exist") is None


class TestHealthSurface:
    def test_health_carries_the_flag_the_banner_polls_on(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The banner decides from /api/health, not from plan().

        plan() shells out to ``systemctl is-active`` and walks the state
        directory — far too expensive for the poll loop, which is why the
        cheap boolean lives here and the detail is fetched once.
        """
        import json

        monkeypatch.setenv("SWARM_STATE_DIR", str(tmp_path / ".swarm"))

        daemon = MagicMock()
        daemon.workers = []
        daemon.pilot = None
        daemon.pool = None
        daemon.source_tree_state = None
        daemon.start_time = 0.0

        request = make_mocked_request("GET", "/api/health")
        request.app["daemon"] = daemon

        resp = asyncio.run(system.handle_health(request))
        payload = json.loads(resp.body)

        assert payload["relocated"] is False, "an explicit false is what shows the banner"
        assert payload["state_dir"].endswith(".swarm")


class TestBlockedRelocation:
    def test_endpoint_refuses_before_launching_the_helper(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, spawns: list, helper_ok: Path
    ) -> None:
        """Both directories exist — relocate() would kill the hive, then fail.

        The endpoint must refuse without spawning anything, because the
        helper stops the service as its first real step.
        """
        source = tmp_path / ".swarm"
        target = tmp_path / ".swarm-legacy"
        source.mkdir()
        target.mkdir()
        blocked = rl.RelocationPlan(
            source=source,
            target=target,
            move_needed=True,
            old_unit=None,
            new_unit=tmp_path / "swarm-legacy.service",
            unit_active=False,
            target_exists=True,
        )
        assert blocked.blocked_reason is not None, "positive control"
        monkeypatch.setattr(rl, "plan", lambda: blocked)

        resp = asyncio.run(system.handle_relocate(_post()))

        assert resp.status == 409
        assert spawns == [], "nothing may be launched for a move that cannot succeed"

    def test_status_payload_carries_the_reason(self, tmp_path: Path) -> None:
        """The banner needs the reason to drop its own button."""
        source = tmp_path / ".swarm"
        target = tmp_path / ".swarm-legacy"
        source.mkdir()
        target.mkdir()
        payload = system._relocation_payload(
            rl.RelocationPlan(
                source=source,
                target=target,
                move_needed=True,
                old_unit=None,
                new_unit=tmp_path / "swarm-legacy.service",
                unit_active=False,
                target_exists=True,
            )
        )

        assert payload["blocked_reason"] is not None
        assert "merge two hives" in str(payload["blocked_reason"])

    def test_ordinary_plan_has_no_reason(self, tmp_path: Path) -> None:
        source = tmp_path / ".swarm"
        source.mkdir()
        payload = system._relocation_payload(
            rl.RelocationPlan(
                source=source,
                target=tmp_path / ".swarm-legacy",
                move_needed=True,
                old_unit=None,
                new_unit=tmp_path / "swarm-legacy.service",
                unit_active=False,
                target_exists=False,
            )
        )
        assert payload["blocked_reason"] is None
