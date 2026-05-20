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
