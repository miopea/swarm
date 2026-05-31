"""Dedicated verifier role — read-only headless judge of task completions.

Item 4 of the 10-repo research bundle (plan
``~/.claude/plans/sequential-churning-meerkat.md``).

The verifier is a **separate** ``claude -p`` subprocess from the headless
Queen, deliberately so. The headless Queen's prompt
(``HEADLESS_DECISION_PROMPT``) is intentionally tight and tactical — fast
yes/no decisions for oversight, escalation, prolonged-BUZZING analysis,
and auto-assignment. Adding "judge whether a diff matches acceptance
criteria" to that prompt would dilute its focus, and per
``docs/specs/headless-queen-architecture.md`` we're explicitly trying
to keep the Queen's role narrow.

So: same mechanism (``claude -p`` subprocess), different role. The
verifier reads only the task spec, the diff, the resolution, and the
worker's notes — Read/Glob/Grep tools — and returns one of three
verdicts:

* ``VERIFIED`` — diff plausibly matches acceptance criteria, or no
  objective criteria were given (default-pass).
* ``UNCERTAIN`` — verifier can't tell. Treated as PASS by callers
  with the reason logged so an operator can audit.
* ``FAILED`` — diff doesn't match the criteria. Caller reopens the task.

No Bash. No test execution. Workers are responsible for running
``/check`` themselves before completing — the tier-1 deterministic
drone catches workers who didn't. The verifier is a content judge,
not a CI re-runner.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from swarm.logging import get_logger
from swarm.queen.json_extract import extract_json as _extract_json

if TYPE_CHECKING:
    from swarm.providers.base import LLMProvider


_log = get_logger("queen.verifier")

# Verifier subprocess timeout. Verification is pure-read judgment —
# 2 minutes is plenty even for chunky diffs.
_VERIFIER_TIMEOUT = 120

# Tools the verifier subprocess is allowed to use. READ-ONLY. No Bash,
# no Write/Edit, no test execution. Workers must run /check themselves
# (tier-1 drone enforces evidence) — the verifier judges *content*.
_VERIFIER_ALLOWED_TOOLS = "Read,Glob,Grep"


VERIFIER_PROMPT = """\
You are the verifier — a stateless judge that decides whether a worker's
completed task actually matches its acceptance criteria. You are NOT
the Queen. You are not approving plans, choosing between options, or
giving advice. You judge one diff against one task spec and return one
verdict.

## Your inputs

You will be given:

- The task title, description, and any explicit acceptance criteria.
- The git diff produced during the task (as text).
- The worker's resolution summary (what they say they did).
- Optional context — any peer warnings or related findings.

## Your tools

Read, Glob, Grep ONLY. Read-only. You can inspect any file in the
repo to corroborate the diff. You cannot run code, run tests, run
shell commands, or modify anything. If you find yourself wanting to
``Bash``, you have already exceeded your role — return UNCERTAIN with
a reason.

## Your output

Return a SINGLE JSON object with these fields:

```json
{
  "verdict": "VERIFIED" | "UNCERTAIN" | "FAILED",
  "reason": "<one-sentence rationale, <= 240 chars>",
  "criteria": [
    {"text": "<verbatim acceptance criterion>", "passed": true | false}
  ]
}
```

The ``criteria`` array is REQUIRED when the task lists explicit
acceptance criteria (you'll see them under "Acceptance criteria" in
the task spec) — one entry per criterion, in the same order, with the
``text`` copied verbatim and ``passed`` set to whether the diff
satisfies it. The array MAY be omitted (or empty) when the task has
no explicit acceptance criteria.

When you mark any criterion ``passed: false``, the overall ``verdict``
MUST be ``FAILED`` — partial successes are still failures. Conversely,
``FAILED`` with all criteria ``passed: true`` is contradictory and
will be treated as ``ERROR``.

No markdown preamble, no commentary, no explanation outside the JSON.

## Verdict rules

- **VERIFIED** — the diff plausibly addresses the task's acceptance
  criteria. Don't demand perfection; you're not a reviewer doing
  line-level QA. If the spirit of the work is present and the
  resolution lines up with the diff, VERIFIED.
- **VERIFIED (default-pass)** — if the task has NO objective
  acceptance criteria (chore, exploration, "investigate X"),
  return VERIFIED with reason `"no objective criteria, accepting
  completion as reported"`. This is the conservative default; the
  drone surfaces UNCERTAIN+VERIFIED equally to operators who can
  audit.
- **FAILED** — the diff clearly doesn't match the spec. Examples:
  task says "add field X" but the diff adds field Y; task says
  "fix bug in foo()" but ``foo()`` is unchanged; task lists
  acceptance criteria the diff plainly contradicts.
- **UNCERTAIN** — you genuinely can't tell. Use sparingly. Callers
  treat UNCERTAIN as PASS with the reason logged, so abuse here
  becomes silent rubber-stamping.

## Don't

- Don't re-run tests or evaluate code quality. The worker ran
  ``/check`` (or the tier-1 drone reopened them already).
- Don't critique style, naming, or architecture. You're judging
  whether the WORK was done, not whether it was done well.
- Don't invent acceptance criteria the task didn't state. If the
  spec is vague, default-pass.
- Don't write more than the JSON. Anything else gets parsed away.
"""


@dataclass
class VerifierVerdict:
    """Outcome of one verifier subprocess call.

    ``verdict`` is the canonical ``VERIFIED | UNCERTAIN | FAILED``
    string from the LLM (or ``"ERROR"`` if the call itself failed).
    ``reason`` is the model's one-sentence rationale (or an error
    message). ``raw`` carries the unparsed LLM output for forensic
    logging when the parse step fails.

    ``criteria_results`` carries the per-criterion verdicts the LLM
    returned in its optional ``criteria`` array. Each entry is
    ``{"text": <criterion>, "passed": <bool>}``. Empty when the task
    had no explicit criteria, or when the LLM omitted the array.
    """

    verdict: str
    reason: str
    raw: str = ""
    criteria_results: list[dict] = field(default_factory=list)

    @property
    def is_pass(self) -> bool:
        """VERIFIED and UNCERTAIN both pass; FAILED reopens; ERROR is treated as PASS."""
        return self.verdict in {"VERIFIED", "UNCERTAIN", "ERROR"}

    @property
    def is_failed(self) -> bool:
        return self.verdict == "FAILED"


class VerifierClient:
    """Run the read-only verifier subprocess on demand.

    Stateless — every call spawns a fresh ``claude -p`` subprocess.
    No session reuse, no rate limiting (caller debounces upstream
    via the self-loop guard on the task itself).
    """

    def __init__(self, provider: LLMProvider | None = None) -> None:
        if provider is None:
            from swarm.providers import get_provider

            provider = get_provider()
        self._provider = provider

    async def verify(
        self,
        *,
        task_title: str,
        task_description: str,
        acceptance_criteria: list[str],
        diff: str,
        resolution: str,
        peer_warnings: str = "",
        cwd: str | None = None,
    ) -> VerifierVerdict:
        """Run one verification and return a structured verdict.

        ``cwd`` is the working directory passed to the subprocess so
        the verifier's Read/Glob/Grep tools resolve relative paths
        against the worker's repo. Defaults to the daemon's CWD when
        unset.
        """
        prompt = _build_prompt(
            title=task_title,
            description=task_description,
            criteria=acceptance_criteria,
            diff=diff,
            resolution=resolution,
            peer_warnings=peer_warnings,
        )
        args = self._provider.headless_command(prompt, output_format="json")
        # Best-effort: append --allowedTools to the claude CLI invocation.
        # Other providers don't support this flag, but the verifier role
        # is currently claude-only; non-claude providers won't be invoked
        # because the verifier drone gates on the worker's provider.
        args = [*args, "--allowedTools", _VERIFIER_ALLOWED_TOOLS]
        return await self._run(args, cwd=cwd)

    async def _run(self, args: list[str], *, cwd: str | None) -> VerifierVerdict:
        env = self._clean_env()
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                cwd=cwd,
            )
        except OSError as e:
            _log.warning("verifier subprocess failed to spawn: %s", e)
            return VerifierVerdict(verdict="ERROR", reason=f"spawn failed: {e}")
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=_VERIFIER_TIMEOUT)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return VerifierVerdict(
                verdict="ERROR",
                reason=f"verifier timed out after {_VERIFIER_TIMEOUT}s",
            )
        except asyncio.CancelledError:
            proc.kill()
            await proc.wait()
            raise
        if proc.returncode and proc.returncode != 0:
            return VerifierVerdict(
                verdict="ERROR",
                reason=f"verifier exited with code {proc.returncode}",
                raw=stderr.decode(errors="replace")[:500],
            )
        text, _session = self._provider.parse_headless_response(stdout)
        return _parse_verdict(text)

    def _clean_env(self) -> dict[str, str]:
        prefixes = self._provider.env_strip_prefixes()
        return {k: v for k, v in os.environ.items() if not any(k.startswith(p) for p in prefixes)}


def _build_prompt(
    *,
    title: str,
    description: str,
    criteria: list[str],
    diff: str,
    resolution: str,
    peer_warnings: str,
) -> str:
    """Assemble the user-message body the verifier sees.

    The system role lives in :data:`VERIFIER_PROMPT` (passed via the
    provider's ``headless_command`` system-prompt slot when the CLI
    supports it; otherwise prepended here as a leading block).
    """
    parts: list[str] = [VERIFIER_PROMPT.strip(), "", "## Task spec", "", f"**Title:** {title}"]
    if description.strip():
        parts.append(f"**Description:** {description.strip()}")
    cleaned_criteria = [c for c in criteria if c.strip()]
    if cleaned_criteria:
        parts.append("**Acceptance criteria:**")
        parts.extend(f"- {c}" for c in cleaned_criteria)
        parts.extend(
            [
                "",
                "Return a `criteria` array in your JSON output with one entry per "
                "criterion above (same order, `text` copied verbatim, `passed` set "
                "to whether the diff satisfies it).",
            ]
        )
    parts.extend(["", "## Worker's resolution", "", resolution.strip() or "(none)"])
    if peer_warnings.strip():
        parts.extend(["", "## Peer warnings", "", peer_warnings.strip()])
    parts.extend(
        [
            "",
            "## Git diff",
            "",
            "```diff",
            diff.strip() or "(empty)",
            "```",
            "",
            "Return ONLY the JSON envelope. No commentary.",
        ]
    )
    return "\n".join(parts)


def _parse_verdict(text: str) -> VerifierVerdict:
    """Parse the LLM's response into a :class:`VerifierVerdict`.

    Tolerates fenced and bare JSON. Anything that fails parsing is
    returned as an ``ERROR`` verdict with the raw text preserved for
    audit so operators can inspect what went wrong.
    """
    obj = _extract_json(text)
    if obj is None:
        return VerifierVerdict(
            verdict="ERROR",
            reason="verifier response was not valid JSON",
            raw=text[:500],
        )
    raw_verdict = str(obj.get("verdict", "")).strip().upper()
    reason = str(obj.get("reason", "")).strip()[:240]
    if raw_verdict not in {"VERIFIED", "UNCERTAIN", "FAILED"}:
        return VerifierVerdict(
            verdict="ERROR",
            reason=f"unexpected verdict {raw_verdict!r}",
            raw=text[:500],
        )
    criteria_results = _parse_criteria(obj.get("criteria"))
    return VerifierVerdict(
        verdict=raw_verdict,
        reason=reason or "(no reason given)",
        raw=text,
        criteria_results=criteria_results,
    )


def _parse_criteria(value: object) -> list[dict]:
    """Coerce the LLM's ``criteria`` array to a list of well-formed dicts.

    Drops malformed entries silently — the verdict/reason fields are
    the load-bearing contract; per-criterion results are
    advisory. Each surviving entry is normalized to
    ``{"text": str, "passed": bool}``.
    """
    if not isinstance(value, list):
        return []
    out: list[dict] = []
    for entry in value:
        if not isinstance(entry, dict):
            continue
        text = entry.get("text")
        passed = entry.get("passed")
        if not isinstance(text, str) or not isinstance(passed, bool):
            continue
        out.append({"text": text.strip(), "passed": passed})
    return out
