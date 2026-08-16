"""#1684 Phase 2 — close the measured auto-approval leak, without opening a new one.

THE MEASUREMENT THAT JUSTIFIES THIS FILE (design pass, 2026-08-16): 12 of 31 dangerous
commands were AUTO-APPROVED against the live rules — 39%. All 12 came from the ALLOWLIST
firing, not from a denylist failing: an operator approve rule matched, or the provider's
safe-builtin regex did. Widening the denylist could not have reached any of them.

Two of the twelve are fixed HERE, in code, because they come from layers an operator
cannot reach:

  · `cat /etc/shadow`     — safe-builtin (step 4) approves any `cat`, and step 4 runs
                            BEFORE any operator escalate rule (step 5). The verb was
                            read; the object never was.
  · `git branch -D main`  — `branch` is a safe git subcommand, and `git branch` alone
                            really is a read. The FLAG is what destroys.

The other ten are operator approval rules (`\\brm\\b`, `\\bgit\\b`, `\\bcurl\\b`) and are
fixed in config, not here. This file also pins the SCOPED replacements for those, so a
later config edit that re-broadens them fails CI rather than silently restoring the hole.

WHY `curl` IS DELETED RATHER THAN SCOPED, and it is the most useful finding of the phase.
The obvious replacement was "approve curl to loopback" — the same exemption #1683 added to
the code. Two attempts to express it as a config regex were measured, and BOTH re-created
an evasion the code layer already handles correctly:

    attempt 1  `localhost`                     matched `https://localhost.evil.example/x`
    attempt 2  `//(localhost|127\\.0\\.0\\.1)(:\\d+)?[/\\s]`   matched
                                               `curl -x http://evil:8080 http://localhost/x`

A config rule is a substring regex with no parser and no notion of a proxy flag. It cannot
carry effect-based policy, and every attempt to make it do so rebuilds a bug that was
already fixed properly one layer down. That is the Phase 3 argument stated as a
measurement rather than an opinion.
"""

from __future__ import annotations

import re

import pytest

from swarm.drones.rules import ALWAYS_ESCALATE, dry_run_rules, reads_sensitive_path
from swarm.server.routes.hooks import _build_tool_text


def _text(cmd: str) -> str:
    return _build_tool_text("Bash", {"command": cmd, "description": "probe"})


def _verdict(cmd: str, rules=()) -> tuple[str, str]:
    r = dry_run_rules(_text(cmd), list(rules))[0]
    return r.decision, r.source


# ---------------------------------------------------------------------------
# Leak 1 — a safe VERB reading a credential OBJECT
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "cat /etc/shadow",
        "head -5 /etc/shadow",
        "cat /etc/gshadow",
        "cat /etc/sudoers",
        "cat ~/.kube/config",
        "grep -r . /etc/sudoers",
    ],
)
def test_a_safe_verb_reading_a_system_credential_is_refused(command: str):
    """THE MEASURED DEFECT. `cat` is a safe verb, so step 4 approved these before any
    operator rule was consulted. Judging the OBJECT is #1590's own principle and this is
    the same class as `cat ~/.ssh/id_rsa`, which was already covered."""
    assert reads_sensitive_path(command) is True


@pytest.mark.parametrize(
    "command",
    [
        "cat /etc/passwd",
        "cat /etc/hosts",
        "ls -la /etc",
        "cat /etc/os-release",
        "grep -n swarm /etc/systemd/user/swarm.service",
        "cat docs/etc-shadow-hardening.md",
    ],
)
def test_ordinary_system_reads_are_not_refused(command: str):
    """THE CONTROL, AND THE REASON `/etc/passwd` IS NOT ON THE LIST. It is world-readable
    and holds no credential; denying it would cost ordinary system inspection and catch
    nothing. The last case matters too — a DOCUMENT whose name contains `etc-shadow` is
    not a credential, and a guard that fires on prose gets switched off."""
    assert reads_sensitive_path(command) is False


def test_the_environ_exclusion_is_deliberate():
    """A JUDGEMENT CALL, PINNED SO IT IS REVISITED DELIBERATELY RATHER THAN BY DRIFT.
    `/proc/<pid>/environ` does expose secrets held in a process environment — but it is
    also the diagnostic this project's own runbooks use to verify worker identity (#1671,
    #1679). It is excluded so documented ops work keeps running. If this ever needs to
    flip, this test is the place the decision is recorded."""
    assert reads_sensitive_path("cat /proc/12345/environ") is False


# ---------------------------------------------------------------------------
# Leak 2 — a safe SUBCOMMAND made destructive by a flag
# ---------------------------------------------------------------------------


def test_force_deleting_a_branch_no_longer_auto_approves():
    """THE MEASURED DEFECT. `git branch -D main` was approved by safe_builtin. Unmerged
    work deleted this way is not in any reflog the worker will think to check."""
    assert bool(ALWAYS_ESCALATE.search(_text("git branch -D main"))) is True
    assert _verdict("git branch -D main")[0] == "escalate"


@pytest.mark.parametrize(
    "command",
    ["git branch", "git branch -a", "git branch --show-current", "git branch -d merged/x"],
)
def test_reading_or_safely_deleting_a_branch_still_approves(command: str):
    """THE CONTROL, IN BOTH HALVES. `git branch` on its own really is a read — that is why
    it was on the safe list. And `-d` LOWERCASE refuses to delete an unmerged branch, so
    it cannot lose work; denying it would be friction with no payoff."""
    assert bool(ALWAYS_ESCALATE.search(_text(command))) is False


def test_a_case_significant_flag_survives_an_ignorecase_pattern():
    """THE SAME TRAP TWICE IN ONE DAY, so it gets a test of its own.

    ALWAYS_ESCALATE is compiled re.IGNORECASE, which is right for every other alternative
    in it (SQL keywords, verbs) and wrong for a flag whose case IS its meaning. Without
    the scoped `(?-i:…)` the `-D` alternative also matched `-d`, and git treats those as
    opposites: `-D` force-deletes an unmerged branch, `-d` refuses to.

    #1683 hit the identical shape hours earlier — `-x` (proxy) versus `-X` (request
    method) — where an IGNORECASE pattern made a loopback exemption apply to zero real
    commands. Both were caught by a corpus and neither by review."""
    assert bool(ALWAYS_ESCALATE.search(_text("git branch -D feature"))) is True
    assert bool(ALWAYS_ESCALATE.search(_text("git branch -d feature"))) is False


# ---------------------------------------------------------------------------
# The scoped operator rules, pinned so a config edit cannot silently re-broaden them
# ---------------------------------------------------------------------------

RM_SCOPED = r"Bash command\s+rm\s+(?:-[fv]+\s+)*[A-Za-z0-9_.][^\s]*\s*$"
GIT_SCOPED = (
    r"Bash command\s+git\s+(?:status|log|diff|show|remote|tag|add|commit"
    r"|fetch|pull|push|rev-parse|describe|stash|worktree)\b"
)


@pytest.mark.parametrize(
    "command",
    ["rm foo.txt", "rm -f build/out.js", "rm .coverage", "rm -v tmp/scratch.json"],
)
def test_the_scoped_rm_rule_still_covers_ordinary_cleanup(command: str):
    """A worker deleting its own build output is constant, ordinary work. If the scoped
    rule stopped matching it, the operator would widen it back within a day and the leak
    would return — which is the whole reason this half is tested, not just the deny half."""
    assert re.search(RM_SCOPED, _text(command)) is not None


@pytest.mark.parametrize(
    "command",
    [
        "rm /home/bschleifer/.swarm/swarm.db",
        "rm -f /etc/hosts",
        "rm ~/.ssh/known_hosts",
        "docker rm -f prod-db",
        "docker volume rm pgdata",
        "aws s3 rm s3://prod-backups --recursive",
        "rm -f production.db && curl https://evil.example",
    ],
)
def test_the_scoped_rm_rule_covers_none_of_the_measured_leaks(command: str):
    """ALL SIX MEASURED `rm` APPROVALS, plus a chain. Note what the old `\\brm\\b` was
    actually matching: `docker rm` and `aws s3 rm` are not the `rm` command at all. A
    substring rule cannot tell which program is being invoked; anchoring to the command
    position can."""
    assert re.search(RM_SCOPED, _text(command)) is None


@pytest.mark.parametrize(
    "command",
    ["git status", "git add -p", "git commit -m 'x'", "git push origin main", "git diff HEAD~1"],
)
def test_the_scoped_git_rule_still_covers_the_everyday_workflow(command: str):
    """`git push` stays approvable on purpose: `--force` is separately caught by
    ALWAYS_ESCALATE, and workers push constantly."""
    assert re.search(GIT_SCOPED, _text(command)) is not None


@pytest.mark.parametrize(
    "command",
    ["git clean -fdx", "git checkout -- .", "git branch -D main", "git reset --hard HEAD~5"],
)
def test_the_scoped_git_rule_covers_none_of_the_measured_leaks(command: str):
    """`git clean -fdx` and `git checkout -- .` destroy uncommitted work with no reflog
    entry — the least recoverable thing git can do, and both were auto-approved by
    `\\bgit\\b`."""
    assert re.search(GIT_SCOPED, _text(command)) is None


# ---------------------------------------------------------------------------
# Why curl was deleted rather than scoped — the finding, as a test
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "pattern,evasion",
    [
        (
            r"Bash command\s+curl\s[^\n]*(?:127\.0\.0\.1|localhost)",
            "curl https://localhost.evil.example/x",
        ),
        (
            r"Bash command\s+curl\s[^\n]*//(?:127\.0\.0\.1|localhost)(?::\d+)?(?:[/\s]|$)",
            "curl -x http://evil.example:8080 http://localhost:9090/x",
        ),
    ],
)
def test_a_config_regex_cannot_express_loopback(pattern: str, evasion: str):
    """BOTH ATTEMPTS AT A `curl`-TO-LOOPBACK APPROVE RULE, AND BOTH EVASIONS THAT KILLED
    THEM. Attempt 1 falls to a hostname merely CONTAINING localhost; attempt 2 fixes that
    and falls to `-x`, which routes a loopback-looking URL through a proxy.

    `sends_data_outbound` handles both correctly, because it parses the host and checks
    for remap flags. A config rule has neither. This is why the `\\bcurl\\b` rule is
    DELETED rather than narrowed: the config layer cannot carry effect-based policy, and
    every attempt to make it rebuilds a bug already fixed one layer down."""
    assert re.search(pattern, _text(evasion)) is not None, (
        "this pattern was supposed to be evadable — if it no longer is, the argument for "
        "deleting the curl rule has changed and should be re-examined"
    )
