"""ReportGenerator — produces analysis reports from test run logs."""

from __future__ import annotations

import asyncio
import json
import os
import re
import statistics
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from swarm.logging import get_logger
from swarm.paths import state_dir
from swarm.testing.log import TestLogEntry, TestRunLog

_log = get_logger("testing.report")


def _tally_drone(
    entry: TestLogEntry,
    decision_counts: Counter[str],
    rule_hits: Counter[str],
) -> int:
    """Process a drone_decision entry. Returns 1 if uncovered, 0 otherwise.

    Note: ``entry.decision`` uses the canonical uppercase form after the
    case-normalization fix (4419d18).  Prior runs may have stored lowercase
    decisions which masked uncovered counts — the current behavior is correct.
    """
    decision_counts[entry.decision] += 1
    if entry.rule_pattern:
        rule_hits[entry.rule_pattern] += 1
    elif entry.decision.upper() in ("CONTINUE", "ESCALATE") and entry.rule_index == -1:
        if entry.source in ("builtin", "escalation"):
            return 0
        return 1
    return 0


def _tally_operator(
    entry: TestLogEntry,
    operator_latencies: list[float],
    queen_confidences: list[float],
) -> tuple[int, int]:
    """Process an operator_decision entry. Returns (approvals, rejections)."""
    approved = 1 if "approved" in entry.detail else 0
    rejected = 0 if approved else 1
    if entry.decision_latency_ms > 0:
        operator_latencies.append(entry.decision_latency_ms)
    if entry.queen_confidence >= 0:
        queen_confidences.append(entry.queen_confidence)
    return approved, rejected


def _compute_none_streaks(entries: list[TestLogEntry]) -> dict[str, Any]:
    """Compute consecutive "NONE" decision streak statistics."""
    streaks: list[int] = []
    current = 0
    for entry in entries:
        if entry.event_type == "drone_decision" and entry.decision.upper() == "NONE":
            current += 1
        else:
            if current > 0:
                streaks.append(current)
            current = 0
    if current > 0:
        streaks.append(current)

    if not streaks:
        return {"max_streak": 0, "mean_streak": 0.0, "total_streaks": 0}
    return {
        "max_streak": max(streaks),
        "mean_streak": round(statistics.mean(streaks), 1),
        "total_streaks": len(streaks),
    }


def _percentile(values: list[float], pct: float) -> float:
    """Compute a percentile from sorted values (nearest-rank method)."""
    if not values:
        return 0.0
    sorted_v = sorted(values)
    idx = int(len(sorted_v) * pct / 100)
    idx = min(idx, len(sorted_v) - 1)
    return sorted_v[idx]


def _latency_distribution(latencies: list[float]) -> dict[str, float]:
    """Compute min, max, p50, p95 from latency values."""
    if not latencies:
        return {"min": 0.0, "max": 0.0, "p50": 0.0, "p95": 0.0}
    return {
        "min": round(min(latencies), 1),
        "max": round(max(latencies), 1),
        "p50": round(_percentile(latencies, 50), 1),
        "p95": round(_percentile(latencies, 95), 1),
    }


def _confidence_distribution(values: list[float]) -> dict[str, float]:
    """Compute min, max, median from confidence values."""
    if not values:
        return {"min": 0.0, "max": 0.0, "median": 0.0}
    return {
        "min": round(min(values), 3),
        "max": round(max(values), 3),
        "median": round(statistics.median(values), 3),
    }


# Pattern to extract key metrics from prior report summary tables.
_REPORT_METRIC_RE = re.compile(
    r"\| Total log entries \| (\d+) \|"
    r".*?\| Operator approvals \| (\d+) \|"
    r".*?\| Operator rejections \| (\d+) \|"
    r".*?\| Avg operator latency \| ([\d.]+)ms \|"
    r".*?\| Avg Queen confidence \| ([\d.]+) \|",
    re.DOTALL,
)

_REPORT_ID_RE = re.compile(r"\*\*Run ID:\*\* (.+)")


def _classify_entries(
    entries: list[TestLogEntry],
) -> tuple[list[TestLogEntry], list[TestLogEntry], list[TestLogEntry], list[tuple[int, int]]]:
    """Split entries into escalations, operator decisions, other drone, and none-streaks."""
    escalations: list[TestLogEntry] = []
    operator_decisions: list[TestLogEntry] = []
    drone_other: list[TestLogEntry] = []
    none_streaks: list[tuple[int, int]] = []  # (start_idx, length)
    current_start = -1
    current_len = 0

    for i, entry in enumerate(entries):
        if entry.event_type == "operator_decision":
            operator_decisions.append(entry)
        elif entry.event_type == "drone_decision":
            d_upper = entry.decision.upper()
            if d_upper == "ESCALATE":
                escalations.append(entry)
            elif d_upper != "NONE":
                drone_other.append(entry)

        if entry.event_type == "drone_decision" and entry.decision.upper() == "NONE":
            if current_start == -1:
                current_start = i
            current_len += 1
        else:
            if current_len > 0:
                none_streaks.append((current_start, current_len))
            current_start = -1
            current_len = 0
    if current_len > 0:
        none_streaks.append((current_start, current_len))

    return escalations, operator_decisions, drone_other, none_streaks


def _pick_streak_boundaries(
    entries: list[TestLogEntry],
    none_streaks: list[tuple[int, int]],
    budget: int,
) -> list[TestLogEntry]:
    """Pick first+last entry from the top 3 longest none-streaks."""
    result: list[TestLogEntry] = []
    sorted_streaks = sorted(none_streaks, key=lambda s: s[1], reverse=True)[:3]
    remaining = budget
    for start, length in sorted_streaks:
        if remaining <= 0:
            break
        result.append(entries[start])
        remaining -= 1
        if length > 1 and remaining > 0:
            result.append(entries[start + length - 1])
            remaining -= 1
    return result


def _stratified_sample(entries: list[TestLogEntry], max_total: int = 20) -> list[TestLogEntry]:
    """Build a stratified sample that prioritises interesting events.

    Allocation:
    - All escalation events (up to 10)
    - All operator decisions (up to 5)
    - Longest "none" streaks (first + last entry of top 2-3 streaks)
    - Remaining slots filled with diverse drone decisions
    """
    escalations, operator_decisions, drone_other, none_streaks = _classify_entries(entries)

    sample: list[TestLogEntry] = []
    sample.extend(escalations[:10])

    remaining = max_total - len(sample)
    sample.extend(operator_decisions[: min(5, remaining)])

    remaining = max_total - len(sample)
    if remaining > 0 and none_streaks:
        sample.extend(_pick_streak_boundaries(entries, none_streaks, remaining))

    remaining = max_total - len(sample)
    if remaining > 0 and drone_other:
        step = max(1, len(drone_other) // remaining)
        sample.extend(drone_other[::step][:remaining])

    return sample


def _render_infra_table(infra: Any) -> str:
    """Render an ``InfraSnapshot`` as a Markdown table.

    Values that are empty/zero are rendered as ``_not captured_`` so
    the reader sees the field exists but wasn't populated — more
    informative than silently hiding a missing value.
    """
    if infra is None:
        return "_no infrastructure snapshot captured for this run_"

    def _fmt(value: Any) -> str:
        if value in (None, "", 0, []):
            return "_not captured_"
        if isinstance(value, list):
            return ", ".join(str(v) for v in value)
        return str(value)

    rows = [
        ("Model", _fmt(getattr(infra, "model", ""))),
        ("Provider", _fmt(getattr(infra, "provider", ""))),
        ("Worker count", _fmt(getattr(infra, "worker_count", 0))),
        ("Port", _fmt(getattr(infra, "port", 0))),
        ("Claude home", _fmt(getattr(infra, "claude_home", ""))),
        ("Swarm version", _fmt(getattr(infra, "swarm_version", ""))),
        ("Python", _fmt(getattr(infra, "python_version", ""))),
        ("Platform", _fmt(getattr(infra, "platform", ""))),
        ("Env hash", _fmt(getattr(infra, "env_hash", ""))),
        ("Env keys", _fmt(getattr(infra, "env_keys", []))),
    ]
    body = "\n".join(f"| {k} | {v} |" for k, v in rows)
    return f"| Field | Value |\n|-------|-------|\n{body}"


class ReportGenerator:
    """Generates markdown reports with stats and AI-powered suggestions."""

    def __init__(self, test_log: TestRunLog, report_dir: Path | None = None) -> None:
        self._test_log = test_log
        self._report_dir = report_dir or state_dir() / "reports"
        self._report_dir.mkdir(parents=True, exist_ok=True)

    async def generate(self) -> Path:
        """Generate a full test report. Returns the path to the markdown file."""
        stats = self._compute_stats()
        analysis = await self._run_analysis(stats)
        return self._write_report(stats, analysis)

    def report_exists(self) -> bool:
        """Check if a report has already been written for this test run."""
        report_path = self._report_dir / f"test-run-{self._test_log.run_id}.md"
        return report_path.exists()

    async def generate_if_pending(self) -> Path | None:
        """Generate a report only if one hasn't been written yet.

        Called on daemon shutdown as a fallback — ensures every test run
        produces a report even if hive_complete never fired.
        Returns the report path, or None if a report already existed.
        """
        if self.report_exists():
            _log.info("report already exists for run %s — skipping", self._test_log.run_id)
            return None
        if not self._test_log.entries:
            _log.info("no entries for run %s — skipping report", self._test_log.run_id)
            return None
        _log.info("generating fallback report for run %s", self._test_log.run_id)
        return await self.generate()

    def _compute_stats(self) -> dict[str, Any]:
        """Compute summary statistics from the test log entries."""
        entries = self._test_log.entries

        decision_counts: Counter[str] = Counter()
        rule_hits: Counter[str] = Counter()
        uncovered_decisions = 0

        operator_approve_count = 0
        operator_reject_count = 0
        operator_latencies: list[float] = []

        state_changes: Counter[str] = Counter()
        queen_confidences: list[float] = []

        for entry in entries:
            if entry.event_type == "drone_decision":
                uncovered_decisions += _tally_drone(entry, decision_counts, rule_hits)
            elif entry.event_type == "operator_decision":
                a, r = _tally_operator(entry, operator_latencies, queen_confidences)
                operator_approve_count += a
                operator_reject_count += r
            elif entry.event_type == "state_change":
                state_changes[entry.detail] += 1
            elif entry.event_type == "queen_analysis":
                if entry.queen_confidence >= 0:
                    queen_confidences.append(entry.queen_confidence)

        avg_op_lat = (
            sum(operator_latencies) / len(operator_latencies) if operator_latencies else 0.0
        )
        avg_q_conf = sum(queen_confidences) / len(queen_confidences) if queen_confidences else 0.0

        return {
            "total_entries": len(entries),
            "decision_counts": dict(decision_counts),
            "rule_hits": dict(rule_hits),
            "uncovered_decisions": uncovered_decisions,
            "operator_approve_count": operator_approve_count,
            "operator_reject_count": operator_reject_count,
            "avg_operator_latency_ms": round(avg_op_lat, 1),
            "operator_latency_dist": _latency_distribution(operator_latencies),
            "state_changes": dict(state_changes),
            "avg_queen_confidence": round(avg_q_conf, 3),
            "queen_confidence_values": queen_confidences,
            "queen_confidence_dist": _confidence_distribution(queen_confidences),
            "none_streaks": _compute_none_streaks(entries),
        }

    def _load_previous_stats(self) -> list[dict[str, Any]]:
        """Scan report directory for prior reports and extract summary metrics.

        Returns a list of dicts (oldest first, up to 5 most recent excluding
        the current run), each containing extracted metrics from the summary table.
        """
        current_id = self._test_log.run_id
        results: list[tuple[str, dict[str, Any]]] = []

        for path in sorted(self._report_dir.glob("test-run-*.md")):
            try:
                text = path.read_text()
            except OSError:
                continue

            # Skip the current run
            id_match = _REPORT_ID_RE.search(text)
            if id_match and id_match.group(1).strip() == current_id:
                continue

            m = _REPORT_METRIC_RE.search(text)
            if not m:
                continue

            results.append(
                (
                    path.stem,
                    {
                        "run": id_match.group(1).strip() if id_match else path.stem,
                        "entries": int(m.group(1)),
                        "approvals": int(m.group(2)),
                        "rejections": int(m.group(3)),
                        "avg_latency_ms": float(m.group(4)),
                        "avg_confidence": float(m.group(5)),
                    },
                )
            )

        # Return the most recent 5
        return [s for _, s in results[-5:]]

    async def _run_analysis(self, stats: dict[str, Any]) -> str:
        """Run AI analysis on the stats and sample log entries.

        Uses ``claude -p`` for actionable suggestions. Falls back to
        a placeholder if claude is not available.
        """
        entries = self._test_log.entries
        sample_entries = _stratified_sample(entries, max_total=20)

        sample_json = json.dumps(
            [
                {
                    "event_type": e.event_type,
                    "worker": e.worker_name,
                    "decision": e.decision,
                    "reason": e.detail,
                    "rule_pattern": e.rule_pattern,
                    "rule_index": e.rule_index,
                    "queen_confidence": e.queen_confidence,
                }
                for e in sample_entries
            ],
            indent=2,
        )

        prompt = self._build_analysis_prompt(stats, sample_json)

        # Use the configured provider for headless analysis
        from swarm.providers import get_provider

        provider = get_provider()
        args = provider.headless_command(prompt, output_format="text")
        prefixes = provider.env_strip_prefixes()
        clean_env = {
            k: v for k, v in os.environ.items() if not any(k.startswith(p) for p in prefixes)
        }

        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=clean_env,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
            if proc.returncode == 0 and stdout:
                return stdout.decode().strip()
            _log.warning("AI analysis failed (rc=%s): %s", proc.returncode, stderr.decode()[:200])
        except (TimeoutError, FileNotFoundError):
            _log.info("LLM CLI not available for analysis — using placeholder")
        except Exception:
            _log.warning("AI analysis failed", exc_info=True)

        return (
            "_AI analysis unavailable — LLM CLI not found or timed out._\n\n"
            "Review the raw stats above and the JSONL log file for manual analysis."
        )

    @staticmethod
    def _build_analysis_prompt(stats: dict[str, Any], sample_json: str) -> str:
        """Build the prompt for AI analysis."""
        return (
            "You are analyzing a Swarm test run. "
            "Here are the aggregated stats:\n\n"
            f"```json\n{json.dumps(stats, indent=2)}\n```\n\n"
            "And here are sample log entries (stratified: escalations, "
            "operator decisions, none-streak boundaries, diverse drone actions):\n\n"
            f"```json\n{sample_json}\n```\n\n"
            "Provide actionable suggestions in these categories:\n"
            "1. **Rule Changes**: Which approval_rules patterns "
            "should be added, modified, or removed?\n"
            "2. **Threshold Adjustments**: Should escalation_threshold, "
            "auto_resolve_delay, or poll_interval change?\n"
            "3. **Uncovered Patterns**: Which drone decisions had "
            "no matching rule? What patterns would cover them?\n"
            "4. **Queen Prompt Improvements**: Based on confidence "
            "scores and decisions, how could Queen prompts improve?\n"
            "5. **Polling Efficiency**: Analyze the none-streak stats — "
            "are there long idle stretches? Should poll_interval increase?\n"
            "6. **General Observations**: Any other optimizations?\n\n"
            "Be specific and actionable. "
            "Reference actual patterns and numbers from the data."
        )

    def _write_report(self, stats: dict[str, Any], analysis: str) -> Path:
        """Write the markdown report file."""
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        report_path = self._report_dir / f"test-run-{self._test_log.run_id}.md"

        decision_table = "\n".join(f"| {k} | {v} |" for k, v in stats["decision_counts"].items())
        rule_table = "\n".join(f"| `{k}` | {v} |" for k, v in stats["rule_hits"].items())
        state_table = "\n".join(f"| {k} | {v} |" for k, v in stats["state_changes"].items())

        # Latency distribution
        lat = stats["operator_latency_dist"]
        lat_section = (
            f"| Min | {lat['min']}ms |\n"
            f"| p50 | {lat['p50']}ms |\n"
            f"| p95 | {lat['p95']}ms |\n"
            f"| Max | {lat['max']}ms |"
        )

        # Confidence distribution
        conf = stats["queen_confidence_dist"]
        conf_section = (
            f"| Min | {conf['min']} |\n"
            f"| Median | {conf['median']} |\n"
            f"| Mean | {stats['avg_queen_confidence']} |\n"
            f"| Max | {conf['max']} |"
        )

        # None streaks
        ns = stats["none_streaks"]
        streak_section = (
            f"| Total streaks | {ns['total_streaks']} |\n"
            f"| Max streak | {ns['max_streak']} |\n"
            f"| Mean streak | {ns['mean_streak']} |"
        )

        # Cross-run trend table
        prev_runs = self._load_previous_stats()
        trend_section = ""
        if prev_runs:
            trend_rows = "\n".join(
                f"| {r['run'][:20]} | {r['entries']} | {r['approvals']} "
                f"| {r['rejections']} | {r['avg_latency_ms']}ms | {r['avg_confidence']} |"
                for r in prev_runs
            )
            trend_section = f"""
## Cross-Run Trends

| Run | Entries | Approvals | Rejections | Avg Latency | Avg Confidence |
|-----|---------|-----------|------------|-------------|----------------|
{trend_rows}
| **{self._test_log.run_id[:20]}** | **{stats["total_entries"]}** \
| **{stats["operator_approve_count"]}** | **{stats["operator_reject_count"]}** \
| **{stats["avg_operator_latency_ms"]}ms** | **{stats["avg_queen_confidence"]}** |

"""

        infra_table = _render_infra_table(self._test_log.infra)

        content = f"""# Swarm Test Run Report

**Run ID:** {self._test_log.run_id}
**Generated:** {timestamp}
**Log file:** `{self._test_log.log_path}`

---

## Infrastructure Snapshot

{infra_table}

## Summary

| Metric | Value |
|--------|-------|
| Total log entries | {stats["total_entries"]} |
| Uncovered decisions (no rule match) | {stats["uncovered_decisions"]} |
| Operator approvals | {stats["operator_approve_count"]} |
| Operator rejections | {stats["operator_reject_count"]} |
| Avg operator latency | {stats["avg_operator_latency_ms"]}ms |
| Avg Queen confidence | {stats["avg_queen_confidence"]} |

## Decision Distribution

| Decision | Count |
|----------|-------|
{decision_table}

## Rule Hit Distribution

| Pattern | Hits |
|---------|------|
{rule_table}

## State Changes

| Transition | Count |
|------------|-------|
{state_table}

## Polling Efficiency

| Metric | Value |
|--------|-------|
{streak_section}

## Latency Distribution

| Percentile | Value |
|------------|-------|
{lat_section}

## Queen Confidence Distribution

| Metric | Value |
|--------|-------|
{conf_section}
{trend_section}
---

## AI Analysis

{analysis}

---

## Suggested Rule Changes

_Based on the analysis above, consider updating your \
`swarm.yaml` `drones.approval_rules` section._

---

*Report generated by Swarm Test Mode*
"""

        report_path.write_text(content)
        _log.info("report written to %s", report_path)
        return report_path
