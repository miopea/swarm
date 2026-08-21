"""TestRunLog — enriched decision log for test mode analysis."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from swarm.logging import get_logger
from swarm.paths import state_dir
from swarm.testing.config import InfraSnapshot

_log = get_logger("testing.log")


@dataclass
class TestLogEntry:
    """A single enriched log entry captured during a test run."""

    timestamp: float = field(default_factory=time.time)
    event_type: str = ""  # drone_decision, queen_analysis, operator_decision, state_change
    worker_name: str = ""
    detail: str = ""
    worker_output: str = ""
    rule_pattern: str = ""
    rule_index: int = -1
    decision: str = ""  # CONTINUE/REVIVE/ESCALATE/NONE
    source: str = ""  # "builtin", "rule", or "escalation" — decision origin
    queen_reasoning: str = ""
    queen_confidence: float = -1.0
    queen_action: str = ""
    decision_latency_ms: float = 0.0
    worker_idle_seconds: float = 0.0
    operator_actor: str = ""  # "simulated-operator"
    proposal_id: str = ""
    proposal_type: str = ""


class TestRunLog:
    """Captures enriched decision context during a test run.

    Writes entries as JSONL to ``~/.swarm/reports/test-run-{run_id}.jsonl``.
    The first line is a single ``{"infra": {...}}`` record that pins the
    run's model/provider/worker_count/env_hash so later regressions can
    tell whether a result delta is real or infra noise.
    """

    def __init__(
        self,
        run_id: str,
        report_dir: Path | None = None,
        infra: InfraSnapshot | None = None,
    ) -> None:
        self.run_id = run_id
        self.report_dir = report_dir or state_dir() / "reports"
        self.report_dir.mkdir(parents=True, exist_ok=True)
        self._log_path = self.report_dir / f"test-run-{run_id}.jsonl"
        self._entries: list[TestLogEntry] = []
        self.infra: InfraSnapshot = infra or InfraSnapshot()
        self._write_infra_header()

    def _write_infra_header(self) -> None:
        """Persist the infra snapshot as the first line of the JSONL log."""
        try:
            with self._log_path.open("a") as f:
                f.write(json.dumps({"infra": self.infra.as_dict()}) + "\n")
        except OSError:
            _log.debug("failed to write infra header", exc_info=True)

    @property
    def entries(self) -> list[TestLogEntry]:
        return list(self._entries)

    @property
    def log_path(self) -> Path:
        return self._log_path

    def _append(self, entry: TestLogEntry) -> None:
        self._entries.append(entry)
        try:
            with self._log_path.open("a") as f:
                f.write(json.dumps(asdict(entry)) + "\n")
        except OSError:
            _log.debug("failed to write test log entry", exc_info=True)

    def record_drone_decision(
        self,
        worker_name: str,
        content: str,
        decision: str,
        reason: str = "",
        rule_pattern: str = "",
        rule_index: int = -1,
        source: str = "",
    ) -> None:
        self._append(
            TestLogEntry(
                event_type="drone_decision",
                worker_name=worker_name,
                detail=reason,
                worker_output=content,
                decision=decision,
                rule_pattern=rule_pattern,
                rule_index=rule_index,
                source=source,
            )
        )

    def record_operator_decision(
        self,
        proposal_id: str,
        proposal_type: str,
        worker_name: str,
        approved: bool,
        reasoning: str = "",
        confidence: float = -1.0,
        latency_ms: float = 0.0,
    ) -> None:
        self._append(
            TestLogEntry(
                event_type="operator_decision",
                worker_name=worker_name,
                detail=f"{'approved' if approved else 'rejected'}: {reasoning}",
                operator_actor="simulated-operator",
                proposal_id=proposal_id,
                proposal_type=proposal_type,
                queen_reasoning=reasoning,
                queen_confidence=confidence,
                decision_latency_ms=latency_ms,
            )
        )

    def record_state_change(
        self,
        worker_name: str,
        old_state: str,
        new_state: str,
    ) -> None:
        self._append(
            TestLogEntry(
                event_type="state_change",
                worker_name=worker_name,
                detail=f"{old_state} -> {new_state}",
            )
        )

    def record_queen_analysis(
        self,
        worker_name: str,
        action: str,
        reasoning: str = "",
        confidence: float = -1.0,
    ) -> None:
        self._append(
            TestLogEntry(
                event_type="queen_analysis",
                worker_name=worker_name,
                detail=reasoning,
                queen_action=action,
                queen_reasoning=reasoning,
                queen_confidence=confidence,
            )
        )
