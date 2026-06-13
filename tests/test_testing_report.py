"""Tests for testing/report.py — ReportGenerator."""

from __future__ import annotations

import pytest

from swarm.testing.config import InfraSnapshot
from swarm.testing.log import TestLogEntry, TestRunLog
from swarm.testing.report import (
    ReportGenerator,
    _compute_none_streaks,
    _confidence_distribution,
    _latency_distribution,
    _render_infra_table,
    _stratified_sample,
)


class TestRenderInfraTable:
    def test_none_infra(self):
        assert "no infrastructure snapshot" in _render_infra_table(None).lower()

    def test_empty_values_marked_as_not_captured(self):
        table = _render_infra_table(InfraSnapshot())
        assert "_not captured_" in table
        # Headers always present
        assert "Model" in table
        assert "Worker count" in table

    def test_populated_fields_render(self):
        snap = InfraSnapshot(
            model="claude-opus-4-7",
            provider="claude",
            worker_count=3,
            port=9091,
            env_hash="abc123def456",
            env_keys=["CLAUDE_MODEL", "SWARM_PROVIDER"],
        )
        table = _render_infra_table(snap)
        assert "claude-opus-4-7" in table
        assert "| 3 |" in table  # worker_count
        assert "| 9091 |" in table
        assert "abc123def456" in table
        assert "CLAUDE_MODEL" in table
        assert "SWARM_PROVIDER" in table


class TestReportRendersInfraSection:
    @pytest.mark.asyncio
    async def test_report_includes_infra_section(self, tmp_path):
        infra = InfraSnapshot(model="claude-opus-4-7", worker_count=2, port=9091)
        log = TestRunLog("infratest", tmp_path, infra=infra)
        log.record_drone_decision("api", "content", "CONTINUE", "ok", "Read", 0)

        gen = ReportGenerator(log, tmp_path)
        # _mock_analysis: without it generate_if_pending shells out to a
        # real `claude -p`, which hangs in sandboxed/nested-session envs.
        with _mock_analysis():
            report_path = await gen.generate_if_pending()
        assert report_path is not None
        text = report_path.read_text()
        assert "## Infrastructure Snapshot" in text
        assert "claude-opus-4-7" in text


class TestReportGeneratorStats:
    def test_empty_log(self, tmp_path):
        log = TestRunLog("empty", tmp_path)
        gen = ReportGenerator(log, tmp_path)
        stats = gen._compute_stats()
        assert stats["total_entries"] == 0
        assert stats["uncovered_decisions"] == 0
        assert stats["operator_approve_count"] == 0

    def test_decision_counts(self, tmp_path):
        log = TestRunLog("counts", tmp_path)
        log.record_drone_decision("api", "c", "CONTINUE", "r", "Read", 0)
        log.record_drone_decision("api", "c", "CONTINUE", "r", "Write", 1)
        log.record_drone_decision("api", "c", "ESCALATE", "r", "", -1)
        log.record_drone_decision("api", "c", "NONE", "r")

        gen = ReportGenerator(log, tmp_path)
        stats = gen._compute_stats()

        assert stats["decision_counts"]["CONTINUE"] == 2
        assert stats["decision_counts"]["ESCALATE"] == 1
        assert stats["decision_counts"]["NONE"] == 1
        assert stats["rule_hits"]["Read"] == 1
        assert stats["rule_hits"]["Write"] == 1
        assert stats["uncovered_decisions"] == 1

    def test_operator_stats(self, tmp_path):
        log = TestRunLog("ops", tmp_path)
        log.record_operator_decision("p1", "assignment", "api", True, "ok", 0.9, 100.0)
        log.record_operator_decision("p2", "escalation", "web", False, "no", 0.3, 200.0)
        log.record_operator_decision("p3", "completion", "api", True, "done", 0.95, 50.0)

        gen = ReportGenerator(log, tmp_path)
        stats = gen._compute_stats()

        assert stats["operator_approve_count"] == 2
        assert stats["operator_reject_count"] == 1
        assert abs(stats["avg_operator_latency_ms"] - 116.7) < 1.0
        assert abs(stats["avg_queen_confidence"] - 0.717) < 0.01

    def test_state_changes(self, tmp_path):
        log = TestRunLog("states", tmp_path)
        log.record_state_change("api", "BUZZING", "RESTING")
        log.record_state_change("api", "RESTING", "BUZZING")
        log.record_state_change("web", "BUZZING", "RESTING")

        gen = ReportGenerator(log, tmp_path)
        stats = gen._compute_stats()

        assert stats["state_changes"]["BUZZING -> RESTING"] == 2
        assert stats["state_changes"]["RESTING -> BUZZING"] == 1

    def test_none_streaks(self, tmp_path):
        log = TestRunLog("streaks", tmp_path)
        # Streak of 3
        log.record_drone_decision("api", "c", "NONE", "idle")
        log.record_drone_decision("api", "c", "NONE", "idle")
        log.record_drone_decision("api", "c", "NONE", "idle")
        # Break
        log.record_drone_decision("api", "c", "CONTINUE", "approved")
        # Streak of 2
        log.record_drone_decision("api", "c", "NONE", "idle")
        log.record_drone_decision("api", "c", "NONE", "idle")

        gen = ReportGenerator(log, tmp_path)
        stats = gen._compute_stats()

        ns = stats["none_streaks"]
        assert ns["max_streak"] == 3
        assert ns["total_streaks"] == 2
        assert ns["mean_streak"] == 2.5

    def test_latency_distribution(self, tmp_path):
        log = TestRunLog("latency", tmp_path)
        log.record_operator_decision("p1", "a", "api", True, "ok", 0.9, 100.0)
        log.record_operator_decision("p2", "a", "api", True, "ok", 0.8, 200.0)
        log.record_operator_decision("p3", "a", "api", True, "ok", 0.7, 300.0)
        log.record_operator_decision("p4", "a", "api", True, "ok", 0.6, 400.0)

        gen = ReportGenerator(log, tmp_path)
        stats = gen._compute_stats()

        lat = stats["operator_latency_dist"]
        assert lat["min"] == 100.0
        assert lat["max"] == 400.0
        assert lat["p50"] > 0

    def test_confidence_distribution(self, tmp_path):
        log = TestRunLog("conf", tmp_path)
        log.record_operator_decision("p1", "a", "api", True, "ok", 0.3, 100.0)
        log.record_operator_decision("p2", "a", "api", True, "ok", 0.7, 100.0)
        log.record_operator_decision("p3", "a", "api", True, "ok", 0.95, 100.0)

        gen = ReportGenerator(log, tmp_path)
        stats = gen._compute_stats()

        conf = stats["queen_confidence_dist"]
        assert conf["min"] == 0.3
        assert conf["max"] == 0.95
        assert conf["median"] == 0.7


class TestNoneStreaks:
    def test_empty_entries(self):
        assert _compute_none_streaks([])["total_streaks"] == 0

    def test_all_none(self):
        entries = [TestLogEntry(event_type="drone_decision", decision="NONE") for _ in range(5)]
        result = _compute_none_streaks(entries)
        assert result["max_streak"] == 5
        assert result["total_streaks"] == 1

    def test_no_none(self):
        entries = [TestLogEntry(event_type="drone_decision", decision="CONTINUE") for _ in range(3)]
        result = _compute_none_streaks(entries)
        assert result["total_streaks"] == 0

    def test_mixed(self):
        entries = [
            TestLogEntry(event_type="drone_decision", decision="NONE"),
            TestLogEntry(event_type="drone_decision", decision="NONE"),
            TestLogEntry(event_type="drone_decision", decision="CONTINUE"),
            TestLogEntry(event_type="drone_decision", decision="NONE"),
        ]
        result = _compute_none_streaks(entries)
        assert result["max_streak"] == 2
        assert result["total_streaks"] == 2
        assert result["mean_streak"] == 1.5


class TestLatencyDistribution:
    def test_empty(self):
        result = _latency_distribution([])
        assert result == {"min": 0.0, "max": 0.0, "p50": 0.0, "p95": 0.0}

    def test_single_value(self):
        result = _latency_distribution([42.0])
        assert result["min"] == 42.0
        assert result["max"] == 42.0

    def test_multiple_values(self):
        result = _latency_distribution([10.0, 20.0, 30.0, 40.0, 50.0])
        assert result["min"] == 10.0
        assert result["max"] == 50.0
        assert result["p50"] == 30.0


class TestConfidenceDistribution:
    def test_empty(self):
        result = _confidence_distribution([])
        assert result == {"min": 0.0, "max": 0.0, "median": 0.0}

    def test_values(self):
        result = _confidence_distribution([0.3, 0.5, 0.9])
        assert result["min"] == 0.3
        assert result["max"] == 0.9
        assert result["median"] == 0.5


class TestStratifiedSample:
    def test_empty_entries(self):
        assert _stratified_sample([]) == []

    def test_escalations_prioritised(self):
        entries = [
            TestLogEntry(event_type="drone_decision", decision="NONE") for _ in range(50)
        ] + [
            TestLogEntry(event_type="drone_decision", decision="ESCALATE", detail="danger")
            for _ in range(3)
        ]
        sample = _stratified_sample(entries, max_total=10)
        escalation_count = sum(1 for e in sample if e.decision == "ESCALATE")
        assert escalation_count == 3

    def test_operator_decisions_included(self):
        entries = [
            TestLogEntry(event_type="drone_decision", decision="NONE") for _ in range(50)
        ] + [TestLogEntry(event_type="operator_decision", detail="approved: ok") for _ in range(3)]
        sample = _stratified_sample(entries, max_total=10)
        op_count = sum(1 for e in sample if e.event_type == "operator_decision")
        assert op_count == 3

    def test_max_total_respected(self):
        entries = [
            TestLogEntry(event_type="drone_decision", decision="CONTINUE") for _ in range(100)
        ]
        sample = _stratified_sample(entries, max_total=20)
        assert len(sample) <= 20

    def test_none_streak_boundaries(self):
        """Longest none-streak boundaries should appear in sample."""
        entries = (
            [TestLogEntry(event_type="drone_decision", decision="NONE") for _ in range(10)]
            + [TestLogEntry(event_type="drone_decision", decision="CONTINUE")]
            + [TestLogEntry(event_type="drone_decision", decision="NONE") for _ in range(3)]
        )
        sample = _stratified_sample(entries, max_total=20)
        # Should include entries from the long streak
        assert len(sample) > 0


class TestReportGeneratorWrite:
    def test_write_report(self, tmp_path):
        log = TestRunLog("write-test", tmp_path)
        log.record_drone_decision("api", "c", "CONTINUE", "r", "Read", 0)
        log.record_operator_decision("p1", "assignment", "api", True, "ok", 0.9, 100.0)

        gen = ReportGenerator(log, tmp_path)
        stats = gen._compute_stats()
        report_path = gen._write_report(stats, "Test analysis content")

        assert report_path.exists()
        content = report_path.read_text()
        assert "# Swarm Test Run Report" in content
        assert "write-test" in content
        assert "Test analysis content" in content
        assert "CONTINUE" in content
        assert "Polling Efficiency" in content
        assert "Latency Distribution" in content
        assert "Queen Confidence Distribution" in content

    @pytest.mark.asyncio
    async def test_generate_with_no_claude(self, tmp_path):
        """Test that generate() completes even when claude CLI is unavailable."""
        from unittest.mock import patch

        log = TestRunLog("no-claude", tmp_path)
        log.record_drone_decision("api", "c", "CONTINUE", "r")

        gen = ReportGenerator(log, tmp_path)
        # Simulate claude binary not found so we don't spawn a real session
        with patch(
            "swarm.testing.report.asyncio.create_subprocess_exec",
            side_effect=FileNotFoundError("claude not found"),
        ):
            report_path = await gen.generate()

        assert report_path.exists()
        content = report_path.read_text()
        assert "# Swarm Test Run Report" in content


class TestCrossRunTrends:
    """Tests for _load_previous_stats() — cross-run comparison."""

    def test_no_prior_reports(self, tmp_path):
        log = TestRunLog("current", tmp_path)
        gen = ReportGenerator(log, tmp_path)
        assert gen._load_previous_stats() == []

    def test_loads_prior_report(self, tmp_path):
        # Write a fake prior report
        prior_content = """# Swarm Test Run Report

**Run ID:** prior-run-001
**Generated:** 20260101-120000
**Log file:** `/tmp/test.jsonl`

---

## Summary

| Metric | Value |
|--------|-------|
| Total log entries | 42 |
| Uncovered decisions (no rule match) | 3 |
| Operator approvals | 10 |
| Operator rejections | 2 |
| Avg operator latency | 150.5ms |
| Avg Queen confidence | 0.82 |
"""
        (tmp_path / "test-run-prior-run-001.md").write_text(prior_content)

        log = TestRunLog("current-run", tmp_path)
        gen = ReportGenerator(log, tmp_path)
        prev = gen._load_previous_stats()

        assert len(prev) == 1
        assert prev[0]["run"] == "prior-run-001"
        assert prev[0]["entries"] == 42
        assert prev[0]["approvals"] == 10
        assert prev[0]["rejections"] == 2
        assert prev[0]["avg_latency_ms"] == 150.5
        assert prev[0]["avg_confidence"] == 0.82

    def test_excludes_current_run(self, tmp_path):
        """Current run should not appear in previous stats."""
        report_content = """# Swarm Test Run Report

**Run ID:** my-run
**Generated:** 20260101-120000
**Log file:** `/tmp/test.jsonl`

---

## Summary

| Metric | Value |
|--------|-------|
| Total log entries | 10 |
| Uncovered decisions (no rule match) | 0 |
| Operator approvals | 5 |
| Operator rejections | 1 |
| Avg operator latency | 100.0ms |
| Avg Queen confidence | 0.9 |
"""
        (tmp_path / "test-run-my-run.md").write_text(report_content)

        log = TestRunLog("my-run", tmp_path)
        gen = ReportGenerator(log, tmp_path)
        assert gen._load_previous_stats() == []

    def test_max_five_previous(self, tmp_path):
        """Should return at most 5 prior runs."""
        for i in range(8):
            content = f"""# Swarm Test Run Report

**Run ID:** run-{i:03d}
**Generated:** 20260101-12000{i}
**Log file:** `/tmp/test.jsonl`

---

## Summary

| Metric | Value |
|--------|-------|
| Total log entries | {10 + i} |
| Uncovered decisions (no rule match) | 0 |
| Operator approvals | {i} |
| Operator rejections | 0 |
| Avg operator latency | 100.0ms |
| Avg Queen confidence | 0.8 |
"""
            (tmp_path / f"test-run-run-{i:03d}.md").write_text(content)

        log = TestRunLog("current", tmp_path)
        gen = ReportGenerator(log, tmp_path)
        prev = gen._load_previous_stats()
        assert len(prev) == 5

    def test_trend_section_in_report(self, tmp_path):
        """Cross-run trends should appear in the report when prior runs exist."""
        prior_content = """# Swarm Test Run Report

**Run ID:** old-run
**Generated:** 20260101-120000
**Log file:** `/tmp/test.jsonl`

---

## Summary

| Metric | Value |
|--------|-------|
| Total log entries | 50 |
| Uncovered decisions (no rule match) | 1 |
| Operator approvals | 8 |
| Operator rejections | 2 |
| Avg operator latency | 120.0ms |
| Avg Queen confidence | 0.75 |
"""
        (tmp_path / "test-run-old-run.md").write_text(prior_content)

        log = TestRunLog("new-run", tmp_path)
        log.record_drone_decision("api", "c", "CONTINUE", "r")

        gen = ReportGenerator(log, tmp_path)
        stats = gen._compute_stats()
        report_path = gen._write_report(stats, "Analysis")

        content = report_path.read_text()
        assert "Cross-Run Trends" in content
        assert "old-run" in content


def _mock_analysis():
    """Patch _run_analysis to avoid shelling out to claude -p in tests."""
    from unittest.mock import AsyncMock, patch

    return patch.object(
        ReportGenerator,
        "_run_analysis",
        new_callable=AsyncMock,
        return_value="Mocked AI analysis for tests.",
    )


class TestGenerateIfPending:
    """Tests for generate_if_pending() — fallback report on shutdown."""

    @pytest.mark.asyncio
    async def test_generates_when_no_report_exists(self, tmp_path):
        """Should generate a report if one hasn't been written yet."""
        log = TestRunLog("pending-test", tmp_path)
        log.record_drone_decision("api", "c", "CONTINUE", "r")

        gen = ReportGenerator(log, tmp_path)
        with _mock_analysis():
            report_path = await gen.generate_if_pending()

        assert report_path is not None
        assert report_path.exists()
        content = report_path.read_text()
        assert "pending-test" in content

    @pytest.mark.asyncio
    async def test_skips_when_report_already_exists(self, tmp_path):
        """Should return None if a report was already written."""
        log = TestRunLog("already-done", tmp_path)
        log.record_drone_decision("api", "c", "CONTINUE", "r")

        gen = ReportGenerator(log, tmp_path)
        with _mock_analysis():
            # Generate the first report
            first_path = await gen.generate()
            assert first_path.exists()

        # Fallback should skip
        result = await gen.generate_if_pending()
        assert result is None

    @pytest.mark.asyncio
    async def test_skips_when_no_entries(self, tmp_path):
        """Should return None for empty logs (no data to report on)."""
        log = TestRunLog("empty-run", tmp_path)

        gen = ReportGenerator(log, tmp_path)
        result = await gen.generate_if_pending()
        assert result is None

    def test_report_exists_false_initially(self, tmp_path):
        """report_exists() should return False before any report is written."""
        log = TestRunLog("check-exists", tmp_path)
        gen = ReportGenerator(log, tmp_path)
        assert gen.report_exists() is False

    @pytest.mark.asyncio
    async def test_report_exists_true_after_generate(self, tmp_path):
        """report_exists() should return True after a report is generated."""
        log = TestRunLog("check-after", tmp_path)
        log.record_drone_decision("api", "c", "CONTINUE", "r")

        gen = ReportGenerator(log, tmp_path)
        with _mock_analysis():
            await gen.generate()
        assert gen.report_exists() is True
