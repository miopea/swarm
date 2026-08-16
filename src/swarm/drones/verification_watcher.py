"""Daily sweep that RUNS the fleet's verification tools, instead of waiting to be asked.

#1702. Three checkers shipped in one day and none of them ran on its own:

  verify-citations.py        push/PR in claude-team-config, and Jira ticket close
  the Jira close hook        only when a ticket closes, which can be days apart
  verify-branch-containment  weekly in swarm CI (added by #1694 after this was filed)

THAT IS THE STATUS ``measure.py`` HAD. #1681 found it cited as ENFORCING a rule while
being an opt-in CLI with zero callers, and it had been that way for months precisely
because it was available-when-needed. A tool that runs when someone remembers is not a
check.

WHY THIS IS DAEMON-SIDE AND NOT CI, WHICH WAS THE ORIGINAL INSTRUCTION. Measured
2026-08-16 before building anything:

  · The daemon binds 127.0.0.1:9090; ``daemon_url`` and ``tunnel_domain`` are empty and
    there are ZERO self-hosted runners. A GitHub runner cannot reach the Queen at all, so
    "report to the Queen" is unimplementable from CI.
  · ``gh api repos/rcghq/claude-team-config/actions/secrets`` returns total_count 0 —
    FLEET_READ_TOKEN does not exist. The last verify-citations CI run said "PARTIAL
    coverage", ``sources scanned: 1`` of 13, with the rcg-architecture target skipped.
  · Containment in swarm's CI uses ``github.token`` and can only see swarm's own remote.

So ZERO of the three achieve full coverage in CI, and the Queen is unreachable from it.
The operator's box is the only place where every checker can reach its repos AND the
Queen exists. Operator ruling 2026-08-16: daemon-side daily job.

TWO RULES THIS FILE EXISTS TO HOLD.

  SEND ON A FINDING, SEND NOTHING OTHERWISE. A channel that reports "all clear" daily is
  a channel that gets muted, and then the one real finding arrives inside a stream of
  noise. Same reasoning as #1695's close hook.

  A ZERO DENOMINATOR IS A FAILED RUN, NOT A CLEAN ONE. ``citations_found: 0`` or
  ``branches_examined: 0`` means the checker parsed nothing — worse than not running,
  because it looks like success. verify-citations prints its denominators for exactly
  this reason (it once reported 10 findings on a clean tree from an empty pathspec, and
  the line that exposed it was ``files on ref: 0``). This watcher refuses to call such a
  run clean and reports it to the Queen as a BROKEN CHECK.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from swarm.logging import get_logger

_log = get_logger("drones.verification")

# Bounded so a hung git call cannot stall the poll loop. Generous because the containment
# sweep walks every remote branch.
_CHECK_TIMEOUT = 300.0

# WHERE THE CHECKERS LIVE. Overridable because #1691 measured that a checkout can sit on a
# branch that does not contain the script at all — claude-team-config's tree was on
# `ablation` while verify-citations.py existed only on origin/main, and a `find` for it
# returned nothing.
_CITATIONS = os.environ.get(
    "SWARM_CITATION_CHECKER",
    os.path.expanduser("~/projects/rcg/claude-team-config/scripts/verify-citations.py"),
)
_ARCH_REPO = os.environ.get(
    "SWARM_ARCH_REPO", os.path.expanduser("~/projects/rcg/rcg-architecture")
)
_CONTAINMENT = os.environ.get(
    "SWARM_CONTAINMENT_CHECKER",
    str(Path(__file__).resolve().parents[3] / "scripts" / "verify-branch-containment.py"),
)


def default_verification_checks() -> list[tuple[str, list[str], str]]:
    """The checkers this sweep runs, as (label, argv, denominator-kind).

    A checker whose script is MISSING is still returned, so the sweep reports it as a
    broken check rather than quietly running two of three and calling that clean. That
    distinction — "did not run" versus "found nothing" — is the whole point of #1702.
    """
    return [
        (
            "citations",
            ["python3", _CITATIONS, "--standards-repo", _ARCH_REPO, "--json"],
            "citations",
        ),
        (
            "containment",
            ["python3", _CONTAINMENT, "--base", "origin/main", "--remote", "--fail-on", "stale"],
            "containment",
        ),
    ]


def run_check_subprocess(argv: list[str]) -> tuple[int, str]:
    """Run one checker and return (exit_code, combined output).

    Raises on a missing interpreter or script — the caller turns that into a FINDING,
    which is correct: a checker that cannot run has not reported clean.
    """
    script = argv[1] if len(argv) > 1 else ""
    if script and not Path(script).exists():
        raise FileNotFoundError(f"checker not found: {script}")
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=_CHECK_TIMEOUT)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


# A checker is (label, argv, denominator-key). The denominator key names the number that
# makes the run readable; absent or zero => the run measured nothing.
CheckRunner = Callable[[list[str]], "tuple[int, str]"]


@dataclass
class CheckOutcome:
    """One checker's result. `denominator is None` means it could not be read at all."""

    label: str
    exit_code: int
    denominator: int | None
    findings: str
    degraded: bool = False

    @property
    def measured_nothing(self) -> bool:
        """True when the run cannot be believed in either direction."""
        return self.denominator is None or self.denominator == 0

    @property
    def has_finding(self) -> bool:
        # Non-zero exit is a finding. So is a zero denominator — a checker that parsed
        # nothing has not exonerated anything, and silence there is the failure mode.
        return self.exit_code != 0 or self.measured_nothing


@dataclass
class SweepResult:
    outcomes: list[CheckOutcome] = field(default_factory=list)

    @property
    def full_coverage(self) -> int:
        return sum(1 for o in self.outcomes if not o.degraded and not o.measured_nothing)

    @property
    def should_report(self) -> bool:
        return any(o.has_finding for o in self.outcomes)


_DENOM_PATTERNS = {
    "citations": re.compile(r"citations[_ ]found\s*[:=]\s*(\d+)", re.I),
    "containment": re.compile(r"branches[_ ]examined\s*[:=]\s*(\d+)", re.I),
}
# One line per branch classified — the containment checker's implicit denominator.
_RE_BRANCH_VERDICT = re.compile(r"^(?:CONTAINED|NOT CONTAINED|SKIPPED|UNMEASURABLE)\s", re.M)
_DEGRADED = re.compile(r"PARTIAL coverage|--skip-arch|UNREADABLE SOURCE|UNMEASURABLE", re.I)


def parse_denominator(kind: str, output: str) -> int | None:
    """Pull the check's own denominator out of its output.

    Returns None when the number is absent — deliberately distinct from 0. "The checker
    did not print a denominator" and "the checker examined nothing" are different
    failures, and both are reportable, but conflating them would hide a checker whose
    output format changed underneath us.
    """
    # CONTAINMENT PRINTS NO TOTAL, so its denominator is DERIVED from its own per-branch
    # verdicts. Measured 2026-08-16: the real sweep returned a genuine finding (one stale
    # branch) and this function returned None, so the watcher labelled a true positive
    # "BROKEN — MEASURED NOTHING". Demanding the script change format would have been the
    # wrong fix twice over: it belongs to another worker, and a checker that reports one
    # line per branch has already told us how many it examined.
    if kind == "containment":
        verdicts = _RE_BRANCH_VERDICT.findall(output)
        if verdicts:
            return len(verdicts)

    pat = _DENOM_PATTERNS.get(kind)
    if pat is None:
        return None
    # JSON first: --json output is the stable contract, the text is for humans.
    try:
        blob = json.loads(output)
        for key in ("citations_found", "branches_examined"):
            if isinstance(blob, dict) and key in blob:
                return int(blob[key])
    except (ValueError, TypeError):
        pass
    m = pat.search(output)
    return int(m.group(1)) if m else None


def looks_degraded(output: str) -> bool:
    """Did the checker announce reduced coverage? Visible, never silent (#1702 AC4)."""
    return bool(_DEGRADED.search(output))


class VerificationWatcher:
    """Runs the verification checkers on a schedule and reports findings to the Queen.

    Dependency-injected end to end: `run_check` and `notify_queen` are callables, so the
    tests exercise the reporting policy without a subprocess, a git repo or a database.
    """

    def __init__(
        self,
        *,
        checks: list[tuple[str, list[str], str]],
        run_check: CheckRunner,
        notify_queen: Callable[[str], Any] | None = None,
        interval_seconds: float = 86400.0,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._checks = checks
        self._run_check = run_check
        self._notify_queen = notify_queen
        self._interval = float(interval_seconds or 0.0)
        self._clock = clock or time.monotonic
        # None = never run. Distinct from 0.0 so 'due at startup' is a state rather
        # than an accident of arithmetic against a monotonic clock that starts near 0.
        self._last_run: float | None = None

    @property
    def enabled(self) -> bool:
        return self._interval > 0 and bool(self._checks)

    def due(self, now: float | None = None) -> bool:
        if not self.enabled:
            return False
        now = self._clock() if now is None else now
        # RUNS ON THE FIRST TICK AFTER STARTUP. A fleet that restarts daily must still
        # sweep; waiting a full interval from boot means a frequently-restarted daemon
        # never runs the check at all — available-when-needed again, in a new costume.
        if self._last_run is None:
            return True
        return (now - self._last_run) >= self._interval

    def sweep(self, now: float | None = None) -> SweepResult:
        self._last_run = self._clock() if now is None else now
        result = SweepResult()
        for label, argv, kind in self._checks:
            try:
                code, output = self._run_check(argv)
            except Exception as exc:
                # A checker that cannot run has NOT reported clean. WARNING, not debug:
                # an operator at the default level must see that a scheduled check is
                # silently absent, which is the whole defect class this addresses.
                _log.warning("verification: %s could not run: %s", label, exc, exc_info=True)
                result.outcomes.append(
                    CheckOutcome(label, exit_code=-1, denominator=None, findings=str(exc)[:400])
                )
                continue
            result.outcomes.append(
                CheckOutcome(
                    label=label,
                    exit_code=code,
                    denominator=parse_denominator(kind, output),
                    findings=output.strip()[:1500],
                    degraded=looks_degraded(output),
                )
            )
        self._report(result)
        return result

    def _report(self, result: SweepResult) -> None:
        if not result.should_report:
            # SILENCE ON A CLEAN RUN IS THE POINT, not an omission.
            _log.info(
                "verification sweep clean — %d/%d checks at full coverage",
                result.full_coverage,
                len(result.outcomes),
            )
            return

        lines = [
            "SCHEDULED VERIFICATION SWEEP — findings.",
            "",
            f"full coverage: {result.full_coverage} of {len(result.outcomes)} checks",
            "",
        ]
        for o in result.outcomes:
            if o.measured_nothing:
                lines.append(
                    f"  BROKEN  {o.label}: denominator={o.denominator!r} — this check "
                    f"MEASURED NOTHING. Not a clean result; treat as unrun."
                )
            elif o.exit_code != 0:
                lines.append(f"  FINDING {o.label}: exit {o.exit_code}, examined {o.denominator}")
            else:
                lines.append(f"  ok      {o.label}: examined {o.denominator}")
            if o.degraded:
                lines.append("          (DEGRADED COVERAGE — the checker said so itself)")
        for o in result.outcomes:
            if o.has_finding and o.findings:
                lines += ["", f"--- {o.label} ---", o.findings]
        lines += [
            "",
            "These checks verify that cited files EXIST and that branches are contained.",
            "They do NOT verify that any claim made about a file is true.",
        ]
        body = "\n".join(lines)
        _log.warning("verification sweep found issues:\n%s", body[:800])
        if self._notify_queen is None:
            return
        try:
            self._notify_queen(body)
        except Exception:
            _log.warning("verification: could not notify the Queen", exc_info=True)
