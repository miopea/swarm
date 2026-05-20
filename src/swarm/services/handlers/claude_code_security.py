"""Claude Code Security scan service handler.

Wraps the ``claude code security scan`` CLI. The handler:

1. Invokes the CLI against ``target_dir`` with ``--json`` output.
2. Parses the findings array.
3. Filters by severity (optional).
4. Deduplicates findings against a persistent state file so a nightly
   scan doesn't refile the same issue every night.
5. Returns the new findings in ``data`` with a severity → task priority
   mapping applied. Downstream pipeline steps (or the operator) can
   turn them into tasks on the board.

Per Anthropic's *Making frontier cybersecurity capabilities available
to defenders* (Feb 2026), Claude Code Security produces the findings;
Swarm's job is scheduling the scan and surfacing unique, actionable
issues to the right worker.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any, ClassVar

from swarm.logging import get_logger
from swarm.services.registry import ServiceContext, ServiceResult

_log = get_logger("services.claude_code_security")

_DEFAULT_TIMEOUT = 600
_DEFAULT_CMD = ("claude", "code", "security", "scan", "--json")
_DEFAULT_DEDUP_PATH = "~/.swarm/security-scan-state.json"

# Severity from the scanner → Swarm task priority.
_SEVERITY_TO_PRIORITY: dict[str, str] = {
    "critical": "urgent",
    "high": "high",
    "medium": "normal",
    "low": "low",
    "info": "low",
}


class ClaudeCodeSecurity:
    """Run ``claude code security scan`` and return deduplicated findings."""

    description = "Run Claude Code Security scan; emit deduped findings."
    example_config: ClassVar[dict[str, Any]] = {
        "target_dir": "/path/to/repo",
        "timeout": 600,
        "severity_filter": ["high", "critical"],
    }

    async def execute(self, config: dict[str, Any], context: ServiceContext) -> ServiceResult:
        target_dir = config.get("target_dir", "")
        if not target_dir:
            return ServiceResult(success=False, error="target_dir is required")
        target = Path(target_dir).expanduser()
        if not target.exists():
            return ServiceResult(success=False, error=f"target_dir does not exist: {target}")

        cmd = (*tuple(config.get("command") or _DEFAULT_CMD), str(target))
        timeout = float(config.get("timeout", _DEFAULT_TIMEOUT))
        severity_filter = set(config.get("severity_filter") or [])
        dedup_path = Path(
            config.get("dedup_state_path") or _DEFAULT_DEDUP_PATH,
        ).expanduser()

        try:
            stdout = await self._run(cmd, timeout)
        except TimeoutError:
            return ServiceResult(success=False, error=f"scan timed out after {timeout}s")
        except _ScanFailure as exc:
            return ServiceResult(success=False, error=str(exc))

        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError as exc:
            return ServiceResult(success=False, error=f"failed to parse scan JSON: {exc}")

        raw = payload.get("findings") or []
        if not isinstance(raw, list):
            return ServiceResult(success=False, error="scan output missing 'findings' array")

        known_hashes = _load_dedup(dedup_path)
        new_findings: list[dict[str, Any]] = []
        skipped = 0

        for finding in raw:
            severity = str(finding.get("severity", "")).lower()
            if severity_filter and severity not in severity_filter:
                continue
            finding_hash = _hash_finding(finding)
            if finding_hash in known_hashes:
                skipped += 1
                continue
            enriched = _enrich_finding(finding, severity, finding_hash)
            new_findings.append(enriched)
            known_hashes.add(finding_hash)

        _save_dedup(dedup_path, known_hashes)
        _log.info(
            "security scan complete: %d new, %d dedup-skipped, total_raw=%d",
            len(new_findings),
            skipped,
            len(raw),
        )

        return ServiceResult(
            success=True,
            data={
                "total_findings": len(new_findings),
                "new_findings": len(new_findings),
                "skipped_dup": skipped,
                "findings": new_findings,
            },
        )

    async def _run(self, cmd: tuple[str, ...], timeout: float) -> bytes:
        """Run the scan CLI and return stdout; raise ``_ScanFailure`` on non-zero exit."""
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            raise
        if proc.returncode != 0:
            err = stderr.decode("utf-8", errors="replace").strip() or "(no stderr)"
            raise _ScanFailure(f"scan CLI exited with code {proc.returncode}: {err}")
        return stdout


class _ScanFailure(Exception):
    """Internal: non-zero scan CLI exit."""


def _hash_finding(finding: dict[str, Any]) -> str:
    """Stable fingerprint of ``(rule_id, path, line)`` — these are the
    dimensions that identify "the same finding" across scans. Title and
    description are excluded because scanners often rephrase them.
    """
    parts = [
        str(finding.get("rule_id", "")),
        str(finding.get("path", "")),
        str(finding.get("line", "")),
    ]
    payload = "\x00".join(parts).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


def _enrich_finding(finding: dict[str, Any], severity: str, finding_hash: str) -> dict[str, Any]:
    priority = _SEVERITY_TO_PRIORITY.get(severity, "normal")
    return {
        "severity": severity,
        "priority": priority,
        "rule_id": finding.get("rule_id", ""),
        "title": finding.get("title") or f"Security: {finding.get('rule_id', 'finding')}",
        "description": finding.get("description", ""),
        "path": finding.get("path", ""),
        "line": finding.get("line", 0),
        "hash": finding_hash,
    }


def _load_dedup(path: Path) -> set[str]:
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        _log.warning("dedup state unreadable at %s — treating as empty", path)
        return set()
    return set(data.get("hashes") or [])


def _save_dedup(path: Path, hashes: set[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"hashes": sorted(hashes)}
    try:
        path.write_text(json.dumps(payload, indent=2) + "\n")
    except OSError as exc:
        _log.warning("failed to write dedup state to %s: %s", path, exc)
