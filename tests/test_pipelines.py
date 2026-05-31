"""Tests for pipeline models, store, engine, and template loader."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from swarm.pipelines.models import (
    Pipeline,
    PipelineStatus,
    PipelineStep,
    StepStatus,
    StepType,
    pipeline_from_dict,
)
from swarm.pipelines.store import PipelineStore
from swarm.tasks.board import TaskBoard

# ---------------------------------------------------------------------------
# Model tests
# ---------------------------------------------------------------------------


class TestPipelineStep:
    def test_start(self) -> None:
        step = PipelineStep(id="s1", name="Step 1")
        step.start()
        assert step.status == StepStatus.IN_PROGRESS
        assert step.started_at is not None

    def test_complete(self) -> None:
        step = PipelineStep(id="s1", name="Step 1")
        step.complete({"key": "value"})
        assert step.status == StepStatus.COMPLETED
        assert step.result == {"key": "value"}
        assert step.is_terminal

    def test_fail(self) -> None:
        step = PipelineStep(id="s1", name="Step 1")
        step.fail("something broke")
        assert step.status == StepStatus.FAILED
        assert step.error == "something broke"
        assert step.is_terminal

    def test_skip(self) -> None:
        step = PipelineStep(id="s1", name="Step 1")
        step.skip()
        assert step.status == StepStatus.SKIPPED
        assert step.is_terminal


class TestPipeline:
    def _make_pipeline(self) -> Pipeline:
        return Pipeline(
            name="test",
            steps=[
                PipelineStep(id="a", name="Step A"),
                PipelineStep(id="b", name="Step B", depends_on=["a"]),
                PipelineStep(id="c", name="Step C", depends_on=["b"]),
            ],
        )

    def test_ready_steps_initial(self) -> None:
        p = self._make_pipeline()
        ready = p.ready_steps()
        assert len(ready) == 1
        assert ready[0].id == "a"

    def test_advance_marks_ready(self) -> None:
        p = self._make_pipeline()
        newly = p.advance()
        assert len(newly) == 1
        assert newly[0].status == StepStatus.READY

    def test_start_sets_running(self) -> None:
        p = self._make_pipeline()
        p.start()
        assert p.status == PipelineStatus.RUNNING

    def test_step_completion_advances_next(self) -> None:
        p = self._make_pipeline()
        p.start()
        # Complete step a
        p.steps[0].complete()
        newly = p.advance()
        assert any(s.id == "b" for s in newly)

    def test_all_completed_marks_pipeline_completed(self) -> None:
        p = self._make_pipeline()
        p.start()
        for step in p.steps:
            step.complete()
        p.advance()
        assert p.status == PipelineStatus.COMPLETED
        assert p.completed_at is not None

    def test_failed_step_marks_pipeline_failed(self) -> None:
        p = self._make_pipeline()
        p.start()
        p.steps[0].fail("boom")
        p.advance()
        assert p.status == PipelineStatus.FAILED

    def test_pause_and_resume(self) -> None:
        p = self._make_pipeline()
        p.start()
        p.pause()
        assert p.status == PipelineStatus.PAUSED
        p.resume()
        assert p.status == PipelineStatus.RUNNING

    def test_progress(self) -> None:
        p = self._make_pipeline()
        assert p.progress == 0.0
        p.steps[0].complete()
        assert abs(p.progress - 1 / 3) < 0.01

    def test_to_dict_roundtrip(self) -> None:
        p = self._make_pipeline()
        d = p.to_dict()
        p2 = pipeline_from_dict(d)
        assert p2.name == p.name
        assert len(p2.steps) == len(p.steps)
        assert p2.steps[1].depends_on == ["a"]

    def test_parallel_steps(self) -> None:
        """Steps without deps on each other are all ready at once."""
        p = Pipeline(
            name="parallel",
            steps=[
                PipelineStep(id="a", name="A"),
                PipelineStep(id="b", name="B"),
                PipelineStep(id="c", name="C", depends_on=["a", "b"]),
            ],
        )
        p.start()
        ready_ids = {s.id for s in p.ready_steps()}
        assert ready_ids == {"a", "b"}


# ---------------------------------------------------------------------------
# Store tests
# ---------------------------------------------------------------------------


class TestPipelineStore:
    def test_save_load_roundtrip(self, tmp_path: Path) -> None:
        store = PipelineStore(path=tmp_path / "pipelines.json")
        p = Pipeline(
            name="test",
            steps=[PipelineStep(id="s1", name="Step 1")],
        )
        store.save({p.id: p})
        loaded = store.load()
        assert p.id in loaded
        assert loaded[p.id].name == "test"
        assert len(loaded[p.id].steps) == 1

    def test_load_missing_file(self, tmp_path: Path) -> None:
        store = PipelineStore(path=tmp_path / "nope.json")
        assert store.load() == {}

    def test_load_corrupt_file(self, tmp_path: Path) -> None:
        path = tmp_path / "pipelines.json"
        path.write_text("not json!")
        store = PipelineStore(path=path)
        assert store.load() == {}


# ---------------------------------------------------------------------------
# Engine tests
# ---------------------------------------------------------------------------


class TestPipelineEngine:
    def _make_engine(self, tmp_path: Path, task_board: TaskBoard | None = None) -> Any:
        from swarm.pipelines.engine import PipelineEngine

        store = PipelineStore(path=tmp_path / "pipelines.json")
        board = task_board or TaskBoard()
        engine = PipelineEngine(store=store, task_board=board)
        return engine, board

    def test_create_pipeline(self, tmp_path: Path) -> None:
        engine, _ = self._make_engine(tmp_path)
        p = engine.create("My Pipeline", description="test")
        assert p.status == PipelineStatus.DRAFT
        assert engine.get(p.id) is not None

    def test_start_creates_tasks(self, tmp_path: Path) -> None:
        engine, board = self._make_engine(tmp_path)
        p = engine.create(
            "Content Pipeline",
            steps=[
                PipelineStep(id="write", name="Write Script", task_type="content"),
                PipelineStep(
                    id="review",
                    name="Review",
                    depends_on=["write"],
                    task_type="review",
                ),
            ],
        )
        ready = engine.start_pipeline(p.id)
        assert len(ready) == 1
        assert ready[0].id == "write"
        # Task should have been created on the board
        assert len(board.active_tasks) > 0 or len(board.available_tasks) > 0

    def test_complete_step_advances(self, tmp_path: Path) -> None:
        engine, board = self._make_engine(tmp_path)
        p = engine.create(
            "test",
            steps=[
                PipelineStep(id="a", name="A"),
                PipelineStep(id="b", name="B", depends_on=["a"]),
            ],
        )
        engine.start_pipeline(p.id)
        newly_ready = engine.complete_step(p.id, "a")
        assert any(s.id == "b" for s in newly_ready)

    def test_task_completion_triggers_step(self, tmp_path: Path) -> None:
        engine, board = self._make_engine(tmp_path)
        p = engine.create(
            "test",
            steps=[
                PipelineStep(id="a", name="A"),
                PipelineStep(id="b", name="B", depends_on=["a"]),
            ],
        )
        engine.start_pipeline(p.id)
        step_a = engine.get(p.id).get_step("a")
        assert step_a.task_id is not None
        # Simulate task completion
        engine.on_task_completed(step_a.task_id, "done")
        step_a_after = engine.get(p.id).get_step("a")
        assert step_a_after.status == StepStatus.COMPLETED

    def test_remove_pipeline(self, tmp_path: Path) -> None:
        engine, _ = self._make_engine(tmp_path)
        p = engine.create("removable")
        assert engine.remove(p.id)
        assert engine.get(p.id) is None

    def test_list_all(self, tmp_path: Path) -> None:
        engine, _ = self._make_engine(tmp_path)
        engine.create("first")
        engine.create("second")
        assert len(engine.list_all()) == 2

    def test_skip_step(self, tmp_path: Path) -> None:
        engine, _ = self._make_engine(tmp_path)
        p = engine.create(
            "test",
            steps=[
                PipelineStep(id="a", name="A"),
                PipelineStep(id="b", name="B", depends_on=["a"]),
            ],
        )
        engine.start_pipeline(p.id)
        newly_ready = engine.skip_step(p.id, "a")
        assert any(s.id == "b" for s in newly_ready)

    def test_fail_step(self, tmp_path: Path) -> None:
        engine, _ = self._make_engine(tmp_path)
        p = engine.create(
            "test",
            steps=[PipelineStep(id="a", name="A")],
        )
        engine.start_pipeline(p.id)
        engine.fail_step(p.id, "a", error="broken")
        assert engine.get(p.id).status == PipelineStatus.FAILED

    def test_automated_step_not_created_as_task(self, tmp_path: Path) -> None:
        """Automated steps should NOT create tasks on the board."""
        engine, board = self._make_engine(tmp_path)
        p = engine.create(
            "test",
            steps=[
                PipelineStep(
                    id="scrape",
                    name="Scrape",
                    step_type=StepType.AUTOMATED,
                    service="youtube_scraper",
                ),
            ],
        )
        engine.start_pipeline(p.id)
        assert len(board.all_tasks) == 0

    def test_update_pipeline(self, tmp_path: Path) -> None:
        engine, _ = self._make_engine(tmp_path)
        p = engine.create("Original", description="old desc", tags=["a"])
        old_updated = p.updated_at
        result = engine.update(p.id, name="Renamed", description="new desc")
        assert result is not None
        assert result.name == "Renamed"
        assert result.description == "new desc"
        assert result.tags == ["a"]  # unchanged
        assert result.updated_at >= old_updated

    def test_update_pipeline_not_found(self, tmp_path: Path) -> None:
        engine, _ = self._make_engine(tmp_path)
        assert engine.update("nonexistent", name="x") is None

    def test_update_pipeline_partial(self, tmp_path: Path) -> None:
        engine, _ = self._make_engine(tmp_path)
        p = engine.create("Keep Me", description="keep this")
        result = engine.update(p.id, tags=["new-tag"])
        assert result is not None
        assert result.name == "Keep Me"
        assert result.description == "keep this"
        assert result.tags == ["new-tag"]

    def test_update_steps_when_draft(self, tmp_path: Path) -> None:
        """P1: editing the step graph is allowed while the pipeline is DRAFT."""
        engine, _ = self._make_engine(tmp_path)
        p = engine.create("editable", steps=[PipelineStep(id="a", name="A")])
        new_steps = [
            PipelineStep(id="x", name="X"),
            PipelineStep(id="y", name="Y", depends_on=["x"]),
        ]
        result = engine.update(p.id, steps=new_steps)
        assert result is not None
        assert [s.id for s in result.steps] == ["x", "y"]

    def test_update_steps_when_paused(self, tmp_path: Path) -> None:
        """P1: editing the step graph is allowed while the pipeline is PAUSED."""
        engine, _ = self._make_engine(tmp_path)
        p = engine.create("pausable", steps=[PipelineStep(id="a", name="A")])
        engine.start_pipeline(p.id)
        engine.pause_pipeline(p.id)
        result = engine.update(p.id, steps=[PipelineStep(id="b", name="B")])
        assert result is not None
        assert [s.id for s in result.steps] == ["b"]

    def test_update_steps_rejected_when_running(self, tmp_path: Path) -> None:
        """P1: step edits are forbidden once the pipeline is RUNNING."""
        engine, _ = self._make_engine(tmp_path)
        p = engine.create("locked", steps=[PipelineStep(id="a", name="A")])
        engine.start_pipeline(p.id)
        with pytest.raises(ValueError, match="can only be edited"):
            engine.update(p.id, steps=[PipelineStep(id="b", name="B")])

    # -----------------------------------------------------------------
    # P3: retry_step — reset FAILED + cascade-reset FAILED downstream.
    # -----------------------------------------------------------------

    def test_retry_step_resets_failed_step(self, tmp_path: Path) -> None:
        engine, _ = self._make_engine(tmp_path)
        p = engine.create("retry-basic", steps=[PipelineStep(id="a", name="A")])
        engine.start_pipeline(p.id)
        engine.fail_step(p.id, "a", error="kaboom")
        reset = engine.retry_step(p.id, "a")
        assert reset == ["a"]
        step_a = engine.get(p.id).get_step("a")
        assert step_a.status in (StepStatus.PENDING, StepStatus.READY, StepStatus.IN_PROGRESS)
        assert step_a.error == ""
        assert step_a.result == {}

    def test_retry_step_cascades_failed_downstream(self, tmp_path: Path) -> None:
        engine, _ = self._make_engine(tmp_path)
        p = engine.create(
            "cascade",
            steps=[
                PipelineStep(id="a", name="A"),
                PipelineStep(id="b", name="B", depends_on=["a"]),
                PipelineStep(id="c", name="C", depends_on=["b"]),
            ],
        )
        engine.start_pipeline(p.id)
        engine.fail_step(p.id, "a")
        # b and c can't actually have run since a failed — simulate the
        # multi-step-failure case by force-flipping them to FAILED.
        for sid in ("b", "c"):
            engine.get(p.id).get_step(sid).status = StepStatus.FAILED
        reset = engine.retry_step(p.id, "a")
        # All three reset, in order from the cascade walk.
        assert set(reset) == {"a", "b", "c"}
        assert reset[0] == "a"

    def test_retry_step_does_not_touch_skipped_downstream(self, tmp_path: Path) -> None:
        engine, _ = self._make_engine(tmp_path)
        p = engine.create(
            "sticky-skip",
            steps=[
                PipelineStep(id="a", name="A"),
                PipelineStep(id="b", name="B", depends_on=["a"]),
            ],
        )
        engine.start_pipeline(p.id)
        engine.fail_step(p.id, "a")
        # Operator skipped b explicitly — that intent is sticky.
        engine.get(p.id).get_step("b").status = StepStatus.SKIPPED
        reset = engine.retry_step(p.id, "a")
        assert reset == ["a"]
        assert engine.get(p.id).get_step("b").status == StepStatus.SKIPPED

    def test_retry_step_does_not_touch_completed_downstream(self, tmp_path: Path) -> None:
        engine, _ = self._make_engine(tmp_path)
        p = engine.create(
            "no-double",
            steps=[
                PipelineStep(id="a", name="A"),
                PipelineStep(id="b", name="B", depends_on=["a"]),
            ],
        )
        engine.start_pipeline(p.id)
        engine.fail_step(p.id, "a")
        engine.get(p.id).get_step("b").status = StepStatus.COMPLETED
        reset = engine.retry_step(p.id, "a")
        assert reset == ["a"]
        assert engine.get(p.id).get_step("b").status == StepStatus.COMPLETED

    def test_retry_step_rejects_non_failed(self, tmp_path: Path) -> None:
        engine, _ = self._make_engine(tmp_path)
        p = engine.create("not-failed", steps=[PipelineStep(id="a", name="A")])
        engine.start_pipeline(p.id)
        # Step is IN_PROGRESS — not FAILED — so retry must refuse.
        with pytest.raises(ValueError, match="retry only resets FAILED"):
            engine.retry_step(p.id, "a")

    def test_retry_step_rejects_missing_pipeline(self, tmp_path: Path) -> None:
        engine, _ = self._make_engine(tmp_path)
        with pytest.raises(ValueError, match=r"Pipeline.*not found"):
            engine.retry_step("nope", "a")

    def test_retry_step_rejects_missing_step(self, tmp_path: Path) -> None:
        engine, _ = self._make_engine(tmp_path)
        p = engine.create("no-step", steps=[PipelineStep(id="a", name="A")])
        with pytest.raises(ValueError, match=r"Step.*not found"):
            engine.retry_step(p.id, "ghost")

    # -----------------------------------------------------------------
    # Cleanup batch: retry-on-COMPLETED with operator confirmation.
    # -----------------------------------------------------------------

    def test_retry_step_completed_rejected_without_confirmation(self, tmp_path: Path) -> None:
        """Retrying a COMPLETED step without `confirmed=True` must 409 —
        re-running a completed step can double-fire side effects."""
        engine, _ = self._make_engine(tmp_path)
        p = engine.create("done", steps=[PipelineStep(id="a", name="A")])
        engine.start_pipeline(p.id)
        engine.complete_step(p.id, "a")
        with pytest.raises(ValueError, match=r"requires explicit confirmation"):
            engine.retry_step(p.id, "a")

    def test_retry_step_completed_accepted_with_confirmation(self, tmp_path: Path) -> None:
        """confirmed=True opts in to retrying a COMPLETED step. The step
        flips back to PENDING and its transient fields wipe — same as
        the FAILED retry path."""
        engine, _ = self._make_engine(tmp_path)
        p = engine.create("done", steps=[PipelineStep(id="a", name="A")])
        engine.start_pipeline(p.id)
        engine.complete_step(p.id, "a", result={"echo": 1})
        reset = engine.retry_step(p.id, "a", confirmed=True)
        assert reset == ["a"]
        step_a = engine.get(p.id).get_step("a")
        assert step_a.status in (StepStatus.PENDING, StepStatus.READY, StepStatus.IN_PROGRESS)
        assert step_a.result == {}

    def test_retry_step_completed_cascade_only_resets_failed(self, tmp_path: Path) -> None:
        """Even with confirmation, retrying a COMPLETED step does NOT
        cascade-reset its COMPLETED downstream. Only FAILED downstream
        flips. Re-firing a whole COMPLETED subtree is a separate
        decision we deliberately deferred."""
        engine, _ = self._make_engine(tmp_path)
        p = engine.create(
            "mixed",
            steps=[
                PipelineStep(id="a", name="A"),
                PipelineStep(id="b", name="B", depends_on=["a"]),
            ],
        )
        engine.start_pipeline(p.id)
        engine.complete_step(p.id, "a")
        engine.get(p.id).get_step("b").status = StepStatus.COMPLETED
        reset = engine.retry_step(p.id, "a", confirmed=True)
        assert reset == ["a"]
        assert engine.get(p.id).get_step("b").status == StepStatus.COMPLETED

    def test_retry_step_in_progress_never_eligible(self, tmp_path: Path) -> None:
        """In-progress steps don't have a meaningful retry — they're
        still running. confirmed=True doesn't unlock them either."""
        engine, _ = self._make_engine(tmp_path)
        p = engine.create("running", steps=[PipelineStep(id="a", name="A")])
        engine.start_pipeline(p.id)  # step is IN_PROGRESS
        with pytest.raises(ValueError, match=r"in_progress"):
            engine.retry_step(p.id, "a", confirmed=True)


# ---------------------------------------------------------------------------
# P3: route-level checks for the retry endpoint — status-code mapping.
# ---------------------------------------------------------------------------


class TestRetryRoute:
    """Verify the route handler maps engine ValueErrors onto 404 / 409.

    We build a minimal aiohttp app with just the pipelines router and a
    stub `daemon` exposing the engine — avoids the heavyweight daemon
    fixture in test_api.py while still exercising the real handler.
    """

    def _make_app(self, tmp_path: Path):
        from types import SimpleNamespace

        from aiohttp import web

        from swarm.pipelines.engine import PipelineEngine
        from swarm.server.routes import pipelines as pipeline_routes

        engine = PipelineEngine(
            store=PipelineStore(path=tmp_path / "pipelines.json"),
            task_board=TaskBoard(),
        )
        app = web.Application()
        app["daemon"] = SimpleNamespace(pipeline_engine=engine)
        pipeline_routes.register(app)
        return app, engine

    @pytest.mark.asyncio
    async def test_retry_returns_ok_and_reset_list(self, tmp_path: Path) -> None:
        from aiohttp.test_utils import TestClient, TestServer

        app, engine = self._make_app(tmp_path)
        p = engine.create("ok", steps=[PipelineStep(id="a", name="A")])
        engine.start_pipeline(p.id)
        engine.fail_step(p.id, "a")
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(f"/api/pipelines/{p.id}/steps/a/retry")
            assert resp.status == 200
            data = await resp.json()
            assert data["ok"] is True
            assert data["reset"] == ["a"]

    @pytest.mark.asyncio
    async def test_retry_returns_404_for_unknown_pipeline(self, tmp_path: Path) -> None:
        from aiohttp.test_utils import TestClient, TestServer

        app, _ = self._make_app(tmp_path)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/pipelines/ghost/steps/a/retry")
            assert resp.status == 404

    @pytest.mark.asyncio
    async def test_retry_returns_404_for_unknown_step(self, tmp_path: Path) -> None:
        from aiohttp.test_utils import TestClient, TestServer

        app, engine = self._make_app(tmp_path)
        p = engine.create("no-step", steps=[PipelineStep(id="a", name="A")])
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(f"/api/pipelines/{p.id}/steps/ghost/retry")
            assert resp.status == 404

    @pytest.mark.asyncio
    async def test_retry_returns_409_for_non_failed_step(self, tmp_path: Path) -> None:
        from aiohttp.test_utils import TestClient, TestServer

        app, engine = self._make_app(tmp_path)
        p = engine.create("not-failed", steps=[PipelineStep(id="a", name="A")])
        engine.start_pipeline(p.id)  # step is IN_PROGRESS, not FAILED
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(f"/api/pipelines/{p.id}/steps/a/retry")
            assert resp.status == 409

    @pytest.mark.asyncio
    async def test_retry_route_accepts_confirmed_for_completed(self, tmp_path: Path) -> None:
        """Cleanup batch: POST with {confirmed: true} body lets the
        retry path accept a COMPLETED step. Without the flag the same
        request 409s — UI gates this behind an explicit confirmation."""
        from aiohttp.test_utils import TestClient, TestServer

        app, engine = self._make_app(tmp_path)
        p = engine.create("done", steps=[PipelineStep(id="a", name="A")])
        engine.start_pipeline(p.id)
        engine.complete_step(p.id, "a")
        async with TestClient(TestServer(app)) as client:
            # Without confirmation — 409.
            resp = await client.post(f"/api/pipelines/{p.id}/steps/a/retry")
            assert resp.status == 409
            # With confirmation — 200, step resets.
            resp = await client.post(
                f"/api/pipelines/{p.id}/steps/a/retry",
                json={"confirmed": True},
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["ok"] is True
            assert data["reset"] == ["a"]


# ---------------------------------------------------------------------------
# Schedule matching
# ---------------------------------------------------------------------------


class TestScheduleMatching:
    def _make_engine(self, tmp_path):
        from swarm.pipelines.engine import PipelineEngine

        store = PipelineStore(path=tmp_path / "pipelines.json")
        return PipelineEngine(store=store, task_board=TaskBoard())

    def test_exact_match(self, tmp_path):
        engine = self._make_engine(tmp_path)
        import time as _time

        now = _time.localtime()
        assert engine._schedule_matches(f"{now.tm_hour}:{now.tm_min}", now) is True

    def test_wildcard_hour(self, tmp_path):
        engine = self._make_engine(tmp_path)
        import time as _time

        now = _time.localtime()
        assert engine._schedule_matches(f"*:{now.tm_min}", now) is True

    def test_wildcard_minute(self, tmp_path):
        engine = self._make_engine(tmp_path)
        import time as _time

        now = _time.localtime()
        assert engine._schedule_matches(f"{now.tm_hour}:*", now) is True

    def test_no_match(self, tmp_path):
        engine = self._make_engine(tmp_path)
        import time as _time

        now = _time.localtime()
        wrong_hour = (now.tm_hour + 1) % 24
        assert engine._schedule_matches(f"{wrong_hour}:00", now) is False

    def test_invalid_format(self, tmp_path):
        engine = self._make_engine(tmp_path)
        import time as _time

        now = _time.localtime()
        assert engine._schedule_matches("bad", now) is False

    def test_cron_minute_hour_match(self, tmp_path):
        engine = self._make_engine(tmp_path)
        import time as _time

        now = _time.localtime()
        # Standard 5-field cron: "M H * * *"
        cron = f"{now.tm_min} {now.tm_hour} * * *"
        assert engine._schedule_matches(cron, now) is True

    def test_cron_every_minute(self, tmp_path):
        engine = self._make_engine(tmp_path)
        import time as _time

        now = _time.localtime()
        assert engine._schedule_matches("* * * * *", now) is True

    def test_cron_weekday_match(self, tmp_path):
        engine = self._make_engine(tmp_path)
        import time as _time

        now = _time.localtime()
        # Match current weekday (cron: 0=Sunday, Python tm_wday: 0=Monday)
        cron_wday = (now.tm_wday + 1) % 7
        cron = f"{now.tm_min} {now.tm_hour} * * {cron_wday}"
        assert engine._schedule_matches(cron, now) is True

    def test_cron_weekday_no_match(self, tmp_path):
        engine = self._make_engine(tmp_path)
        import time as _time

        now = _time.localtime()
        cron_wday = (now.tm_wday + 1) % 7
        wrong_wday = (cron_wday + 1) % 7
        cron = f"{now.tm_min} {now.tm_hour} * * {wrong_wday}"
        assert engine._schedule_matches(cron, now) is False

    def test_cron_invalid_expression(self, tmp_path):
        engine = self._make_engine(tmp_path)
        import time as _time

        now = _time.localtime()
        # Too many fields, bad numbers, garbage
        assert engine._schedule_matches("60 25 * * *", now) is False
        assert engine._schedule_matches("not a schedule", now) is False

    def test_empty_schedule(self, tmp_path):
        engine = self._make_engine(tmp_path)
        import time as _time

        now = _time.localtime()
        assert engine._schedule_matches("", now) is False
        assert engine._schedule_matches("   ", now) is False

    def test_tz_kwarg_does_not_blow_up_with_unknown_zone(self, tmp_path):
        """P2: an invalid timezone string must NOT crash the matcher —
        operators can typo an IANA name and we degrade to local time."""
        engine = self._make_engine(tmp_path)
        # Truthy schedule + bogus tz: should not raise, should not match
        # the current minute by accident either.
        result = engine._schedule_matches("0 0 1 1 *", tz="Not/A/Zone")
        assert isinstance(result, bool)


# ---------------------------------------------------------------------------
# Schedule helpers (P2 — preview endpoint + builder support)
# ---------------------------------------------------------------------------


class TestScheduleHelpers:
    def test_normalize_hhmm(self):
        from swarm.pipelines.schedule import normalize_schedule

        assert normalize_schedule("14:30") == "30 14 * * *"
        assert normalize_schedule("*:30") == "30 * * * *"
        assert normalize_schedule("14:*") == "* 14 * * *"

    def test_normalize_passes_cron_through(self):
        from swarm.pipelines.schedule import normalize_schedule

        assert normalize_schedule("30 14 * * 1-5") == "30 14 * * 1-5"
        assert normalize_schedule("") == ""

    def test_humanize_daily(self):
        from swarm.pipelines.schedule import humanize_schedule

        assert humanize_schedule("14:30") == "Daily at 14:30"
        assert humanize_schedule("30 14 * * *") == "Daily at 14:30"

    def test_humanize_weekdays(self):
        from swarm.pipelines.schedule import humanize_schedule

        assert humanize_schedule("30 14 * * 1-5") == "Weekdays at 14:30"

    def test_humanize_weekends(self):
        from swarm.pipelines.schedule import humanize_schedule

        assert humanize_schedule("0 9 * * 0,6") == "Weekends at 09:00"

    def test_humanize_specific_day(self):
        from swarm.pipelines.schedule import humanize_schedule

        assert humanize_schedule("0 9 * * 1") == "Every Mon at 09:00"

    def test_humanize_custom_fallback(self):
        from swarm.pipelines.schedule import humanize_schedule

        # Day-of-month rule isn't covered by the preset builder, so we
        # gracefully fall through to "Custom: <expr>" rather than lie.
        assert humanize_schedule("0 9 15 * *").startswith("Custom: ")

    def test_preview_returns_valid_for_legacy_hhmm(self):
        from swarm.pipelines.schedule import preview_schedule

        out = preview_schedule("14:30")
        assert out["valid"] is True
        assert out["human"] == "Daily at 14:30"
        assert len(out["next"]) == 5

    def test_preview_returns_valid_for_cron(self):
        from swarm.pipelines.schedule import preview_schedule

        out = preview_schedule("30 14 * * 1-5", tz="America/New_York")
        assert out["valid"] is True
        assert out["human"] == "Weekdays at 14:30"
        # Sanity check the tz-offset survives — every ISO string should
        # carry an offset, not just a naive datetime.
        for ts in out["next"]:
            assert "-" in ts[10:] or "+" in ts[10:]

    def test_preview_rejects_invalid_cron(self):
        from swarm.pipelines.schedule import preview_schedule

        out = preview_schedule("not a schedule")
        assert out["valid"] is False
        assert "Invalid" in out["error"]

    def test_preview_rejects_unknown_timezone(self):
        from swarm.pipelines.schedule import preview_schedule

        out = preview_schedule("30 14 * * *", tz="Not/A/Zone")
        assert out["valid"] is False
        assert "Unknown timezone" in out["error"]


# ---------------------------------------------------------------------------
# Pipeline.timezone — round-trips through create/update/store
# ---------------------------------------------------------------------------


class TestPipelineTimezone:
    def _make_engine(self, tmp_path: Path):
        from swarm.pipelines.engine import PipelineEngine

        store = PipelineStore(path=tmp_path / "pipelines.json")
        return PipelineEngine(store=store, task_board=TaskBoard())

    def test_create_with_timezone(self, tmp_path: Path) -> None:
        engine = self._make_engine(tmp_path)
        p = engine.create("tz-test", timezone="America/New_York")
        assert p.timezone == "America/New_York"
        # to_dict includes it so the API surface stays consistent.
        assert p.to_dict()["timezone"] == "America/New_York"

    def test_update_timezone(self, tmp_path: Path) -> None:
        engine = self._make_engine(tmp_path)
        p = engine.create("tz-test", timezone="UTC")
        result = engine.update(p.id, timezone="America/Los_Angeles")
        assert result is not None
        assert result.timezone == "America/Los_Angeles"

    def test_update_timezone_allowed_while_running(self, tmp_path: Path) -> None:
        """Timezone updates aren't gated on status — the operator should
        be able to fix a misconfigured zone without pausing first."""
        engine = self._make_engine(tmp_path)
        p = engine.create("tz-test", steps=[PipelineStep(id="a", name="A")])
        engine.start_pipeline(p.id)
        result = engine.update(p.id, timezone="Europe/London")
        assert result is not None
        assert result.timezone == "Europe/London"

    def test_timezone_survives_store_roundtrip(self, tmp_path: Path) -> None:
        from swarm.pipelines.engine import PipelineEngine

        store = PipelineStore(path=tmp_path / "pipelines.json")
        engine1 = PipelineEngine(store=store, task_board=TaskBoard())
        engine1.create("persistent", timezone="Asia/Tokyo")
        # Fresh engine reads from disk — timezone field must survive.
        engine2 = PipelineEngine(
            store=PipelineStore(path=tmp_path / "pipelines.json"),
            task_board=TaskBoard(),
        )
        loaded = list(engine2.list_all())
        assert len(loaded) == 1
        assert loaded[0].timezone == "Asia/Tokyo"


# ---------------------------------------------------------------------------
# Template tests
# ---------------------------------------------------------------------------


class TestPipelineTemplate:
    def test_load_template(self, tmp_path: Path) -> None:
        from swarm.pipelines.template import load_template

        template = tmp_path / "content.yaml"
        template.write_text(
            """
name: content-pipeline
description: A test content pipeline
steps:
  - id: capture
    name: Idea Capture
    type: automated
    service: youtube_scraper
    config:
      channels: [chan1]
  - id: write
    name: Write Script
    type: agent
    task_type: content
    depends_on: [capture]
  - id: film
    name: Filming
    type: human
    depends_on: [write]
"""
        )
        p = load_template("content", str(tmp_path))
        assert p.name == "content-pipeline"
        assert len(p.steps) == 3
        assert p.steps[0].step_type == StepType.AUTOMATED
        assert p.steps[0].service == "youtube_scraper"
        assert p.steps[1].depends_on == ["capture"]
        assert p.steps[2].step_type == StepType.HUMAN
        assert p.template_name == "content"

    def test_load_missing_template(self, tmp_path: Path) -> None:
        from swarm.pipelines.template import load_template

        with pytest.raises(FileNotFoundError):
            load_template("nonexistent", str(tmp_path))


class TestDependencyValidation:
    """A malformed dependency graph (missing or circular depends_on) must fail
    loudly at start, not start RUNNING and hang forever with no runnable step.
    """

    def _engine(self, tmp_path: Path) -> Any:
        from swarm.pipelines.engine import PipelineEngine

        return PipelineEngine(store=PipelineStore(path=tmp_path / "p.json"), task_board=TaskBoard())

    def test_missing_dependency_rejected_at_start(self, tmp_path: Path) -> None:
        engine = self._engine(tmp_path)
        p = engine.create("miss", steps=[PipelineStep(id="x", name="X", depends_on=["ghost"])])
        with pytest.raises(ValueError, match="unknown step"):
            engine.start_pipeline(p.id)

    def test_circular_dependency_rejected_at_start(self, tmp_path: Path) -> None:
        engine = self._engine(tmp_path)
        p = engine.create(
            "circ",
            steps=[
                PipelineStep(id="a", name="A", depends_on=["b"]),
                PipelineStep(id="b", name="B", depends_on=["a"]),
            ],
        )
        with pytest.raises(ValueError, match="circular dependency"):
            engine.start_pipeline(p.id)

    def test_valid_dag_starts_cleanly(self, tmp_path: Path) -> None:
        engine = self._engine(tmp_path)
        p = engine.create(
            "ok",
            steps=[
                PipelineStep(id="a", name="A"),
                PipelineStep(id="b", name="B", depends_on=["a"]),
            ],
        )
        ready = engine.start_pipeline(p.id)
        assert [s.id for s in ready] == ["a"]

    def test_from_dict_null_depends_on_coerced(self) -> None:
        # An explicit null in stored JSON must not crash ready_steps().
        p = pipeline_from_dict(
            {
                "id": "p1",
                "name": "n",
                "steps": [{"id": "a", "name": "A", "depends_on": None}],
            }
        )
        assert p.steps[0].depends_on == []
        # Sanity: advancing doesn't raise.
        p.start()


class TestPipelineRoundTrip:
    def test_every_step_field_survives(self, tmp_path: Path) -> None:
        """Guard: a fully-populated pipeline survives save -> load with all
        step fields intact (mirrors the FileTaskStore round-trip guard)."""
        store = PipelineStore(path=tmp_path / "p.json")
        step = PipelineStep(
            id="s1",
            name="Step One",
            step_type=StepType.AGENT,
            description="desc",
            depends_on=[],
            task_type="content",
            assigned_worker="hub",
            service="svc",
            config={"k": "v"},
            schedule="30 14 * * *",
        )
        step.start()
        step.task_id = "task-123"
        p = Pipeline(
            name="rt", description="d", tags=["t"], timezone="America/New_York", steps=[step]
        )
        store.save({p.id: p})
        loaded = store.load()[p.id]
        s2 = loaded.steps[0]
        assert loaded.timezone == "America/New_York"
        assert loaded.tags == ["t"]
        assert s2.assigned_worker == "hub"
        assert s2.service == "svc"
        assert s2.config == {"k": "v"}
        assert s2.schedule == "30 14 * * *"
        assert s2.task_id == "task-123"
        assert s2.started_at == step.started_at
