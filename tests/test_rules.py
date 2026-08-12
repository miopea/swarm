"""Tests for drones/rules.py — decision logic."""

import time

import pytest

from swarm.config import DroneConfig
from swarm.drones.rules import Decision, decide
from swarm.worker.worker import WorkerState
from tests.conftest import make_worker as _make_worker


@pytest.fixture
def escalated():
    """Provide a fresh escalated dict for each test."""
    return {}


class TestDecideStung:
    def test_stung_worker_gets_revived(self, escalated):
        w = _make_worker(state=WorkerState.STUNG)
        d = decide(w, "$ ", escalated=escalated)
        assert d.decision == Decision.REVIVE
        assert "exited" in d.reason

    def test_stung_preserves_escalation_until_buzzing(self, escalated):
        """STUNG should NOT clear escalation — it clears when worker goes BUZZING."""
        escalated["api"] = time.monotonic()
        w = _make_worker(state=WorkerState.STUNG)
        decide(w, "$ ", escalated=escalated)
        # Escalation stays until worker recovers to BUZZING
        assert "api" in escalated

    def test_buzzing_clears_escalation_after_stung(self, escalated):
        """After STUNG → revive → BUZZING, escalation should be cleared."""
        escalated["api"] = time.monotonic()
        w = _make_worker(state=WorkerState.STUNG)
        decide(w, "$ ", escalated=escalated)
        assert "api" in escalated  # still set during STUNG
        w.state = WorkerState.BUZZING
        decide(w, "esc to interrupt", escalated=escalated)
        assert "api" not in escalated  # cleared by BUZZING


class TestDecideBuzzing:
    def test_buzzing_worker_does_nothing(self, escalated):
        w = _make_worker(state=WorkerState.BUZZING)
        d = decide(w, "esc to interrupt", escalated=escalated)
        assert d.decision == Decision.NONE
        assert "working" in d.reason

    def test_buzzing_without_prompt_returns_none(self, escalated):
        """BUZZING with normal output (no prompts) → NONE unchanged."""
        w = _make_worker(state=WorkerState.BUZZING)
        d = decide(w, "Processing files...\nesc to interrupt", escalated=escalated)
        assert d.decision == Decision.NONE

    def test_buzzing_with_choice_prompt_evaluates_rules(self, escalated):
        """BUZZING + choice prompt in content → evaluates via _decide_idle_state."""
        w = _make_worker(state=WorkerState.BUZZING)
        content = (
            "esc to interrupt\n"
            + """> 1. Always allow
  2. Yes
  3. No
Enter to select · ↑/↓ to navigate"""
        )
        d = decide(w, content, escalated=escalated)
        # Should evaluate the choice (not return NONE)
        assert d.decision in (Decision.CONTINUE, Decision.ESCALATE)

    def test_buzzing_with_plan_prompt_escalates(self, escalated):
        """BUZZING + plan prompt → ESCALATE (plans always need approval)."""
        w = _make_worker(state=WorkerState.BUZZING)
        content = (
            "esc to interrupt\n"
            "Here is my plan:\n"
            "1. Step one\n"
            "2. Step two\n"
            "\n"
            "Do you want me to proceed with this plan?\n"
            "> 1. Yes, proceed\n"
            "  2. No, revise\n"
            "  3. Cancel\n"
            "Enter to select\n"
        )
        d = decide(w, content, escalated=escalated)
        assert d.decision == Decision.ESCALATE
        assert "plan" in d.reason

    def test_buzzing_with_accept_edits_evaluates(self, escalated):
        """BUZZING + accept-edits prompt → evaluates (auto-accepts file edits)."""
        w = _make_worker(state=WorkerState.BUZZING)
        content = (
            "esc to interrupt\n"
            "  src/swarm/worker/state.py\n"
            ">> accept edits on (shift+tab to cycle)\n"
        )
        d = decide(w, content, escalated=escalated)
        assert d.decision == Decision.CONTINUE
        assert "accept edits" in d.reason

    def test_buzzing_escalation_not_cleared_when_prompt_present(self, escalated):
        """BUZZING + prompt + already escalated → escalation preserved."""
        escalated["api"] = time.monotonic()
        w = _make_worker(state=WorkerState.BUZZING)
        content = (
            "esc to interrupt\n"
            "  src/swarm/worker/state.py\n"
            ">> accept edits on · 2 bashes (shift+tab to cycle)\n"
        )
        # accept-edits with bash → ESCALATE, but already escalated
        decide(w, content, escalated=escalated)
        # Key: escalation must NOT be popped
        assert "api" in escalated

    def test_buzzing_clears_escalation_when_no_prompt(self, escalated):
        """BUZZING with no prompts should still clear escalation (existing behavior)."""
        escalated["api"] = time.monotonic()
        w = _make_worker(state=WorkerState.BUZZING)
        decide(w, "esc to interrupt", escalated=escalated)
        assert "api" not in escalated


class TestDecideResting:
    def test_choice_prompt_continues(self, escalated):
        w = _make_worker(state=WorkerState.WAITING)
        content = """> 1. Always allow
  2. Yes
  3. No
Enter to select · ↑/↓ to navigate"""
        d = decide(w, content, escalated=escalated)
        assert d.decision == Decision.CONTINUE
        assert "choice" in d.reason

    def test_user_question_escalates(self, escalated):
        """AskUserQuestion prompts must escalate — never auto-continue."""
        w = _make_worker(state=WorkerState.WAITING)
        content = """\
How would you like to proceed?
> 1. Fix both issues
  2. File issues for later
  3. Done for now
  4. Type something.

  5. Chat about this
Enter to select · ↑/↓ to navigate · Esc to cancel"""
        d = decide(w, content, escalated=escalated)
        assert d.decision == Decision.ESCALATE
        assert "user question" in d.reason

    def test_user_question_only_fires_once(self, escalated):
        """User question escalation should not spam."""
        w = _make_worker(state=WorkerState.WAITING)
        content = """\
Which approach?
> 1. Option A
  2. Option B
  3. Type something.
Enter to select"""
        d1 = decide(w, content, escalated=escalated)
        assert d1.decision == Decision.ESCALATE
        d2 = decide(w, content, escalated=escalated)
        assert d2.decision == Decision.NONE

    def test_empty_prompt_idles(self, escalated):
        w = _make_worker(state=WorkerState.WAITING)
        d = decide(w, "> ", escalated=escalated)
        assert d.decision == Decision.NONE
        assert "idle" in d.reason

    def test_accept_edits_prompt_continues(self, escalated):
        """Accept-edits prompt should auto-accept (CONTINUE)."""
        w = _make_worker(state=WorkerState.WAITING)
        content = (
            "Running /check...\n"
            "  src/swarm/worker/state.py\n"
            ">> accept edits on (shift+tab to cycle)\n"
        )
        d = decide(w, content, escalated=escalated)
        assert d.decision == Decision.CONTINUE
        assert "accept edits" in d.reason

    def test_idle_prompt_does_nothing(self, escalated):
        w = _make_worker(state=WorkerState.RESTING)
        d = decide(w, '> Try "how does auth work"\n? for shortcuts', escalated=escalated)
        assert d.decision == Decision.NONE
        assert "idle" in d.reason

    def test_idle_prompt_with_empty_line_still_blocked(self, escalated):
        """Content with both an empty prompt line AND '? for shortcuts' → NONE.

        Defense-in-depth: idle_prompt is checked before empty_prompt so
        suggestions at the prompt always block auto-continue.
        """
        w = _make_worker(state=WorkerState.RESTING)
        content = "> \n? for shortcuts"
        d = decide(w, content, escalated=escalated)
        assert d.decision == Decision.NONE
        assert "idle" in d.reason

    def test_idle_prompt_ctrl_t_hint(self, escalated):
        """'ctrl+t to hide' hint should also be treated as idle prompt."""
        w = _make_worker(state=WorkerState.RESTING)
        d = decide(w, '> try "how do I log?"\nctrl+t to hide', escalated=escalated)
        assert d.decision == Decision.NONE
        assert "idle" in d.reason

    def test_waiting_worker_goes_through_decide_idle_state(self, escalated):
        """WAITING workers should be handled by _decide_idle_state, same as RESTING."""
        w = _make_worker(state=WorkerState.WAITING)
        content = """> 1. Yes
  2. No
Enter to select"""
        d = decide(w, content, escalated=escalated)
        assert d.decision == Decision.CONTINUE

    def test_unknown_state_escalates_after_threshold(self, escalated):
        cfg = DroneConfig(escalation_threshold=15.0)
        w = _make_worker(
            state=WorkerState.WAITING,
            resting_since=time.time() - 20,
        )
        d = decide(w, "some unknown content without prompts", config=cfg, escalated=escalated)
        assert d.decision == Decision.ESCALATE

    def test_unknown_state_waits_before_threshold(self, escalated):
        cfg = DroneConfig(escalation_threshold=15.0)
        w = _make_worker(
            state=WorkerState.WAITING,
            resting_since=time.time() - 5,
        )
        d = decide(w, "some unknown content without prompts", config=cfg, escalated=escalated)
        assert d.decision == Decision.NONE

    def test_escalation_only_fires_once(self, escalated):
        cfg = DroneConfig(escalation_threshold=15.0)
        w = _make_worker(
            state=WorkerState.WAITING,
            resting_since=time.time() - 20,
        )
        d1 = decide(w, "unknown state", config=cfg, escalated=escalated)
        assert d1.decision == Decision.ESCALATE
        d2 = decide(w, "unknown state", config=cfg, escalated=escalated)
        assert d2.decision == Decision.NONE

    def test_unknown_state_emits_unknown_prompt_event(self, escalated):
        """UNKNOWN_PROMPT event is included when escalating for unrecognized state."""
        cfg = DroneConfig(escalation_threshold=15.0)
        w = _make_worker(
            state=WorkerState.WAITING,
            resting_since=time.time() - 20,
        )
        d = decide(w, "some unknown content without prompts", config=cfg, escalated=escalated)
        assert d.decision == Decision.ESCALATE
        assert d.events is not None
        types = [e.event_type.value for e in d.events]
        assert "unknown_prompt" in types


class TestReviveLimits:
    def test_stung_escalates_after_max_revives(self, escalated):
        cfg = DroneConfig(max_revive_attempts=3)
        w = _make_worker(state=WorkerState.STUNG)
        w.revive_count = 3
        d = decide(w, "$ ", config=cfg, escalated=escalated)
        assert d.decision == Decision.ESCALATE
        assert "crash loop" in d.reason

    def test_stung_revives_when_under_limit(self, escalated):
        cfg = DroneConfig(max_revive_attempts=3)
        w = _make_worker(state=WorkerState.STUNG)
        w.revive_count = 2
        d = decide(w, "$ ", config=cfg, escalated=escalated)
        assert d.decision == Decision.REVIVE

    def test_crash_loop_escalation_fires_only_once(self, escalated):
        """Regression: STUNG with exhausted revives should escalate once, then NONE.

        Previously, _esc.discard() at the top of the STUNG branch undid
        the _esc.add() from the previous cycle, causing infinite re-escalation.
        """
        cfg = DroneConfig(max_revive_attempts=3)
        w = _make_worker(state=WorkerState.STUNG)
        w.revive_count = 3
        d1 = decide(w, "$ ", config=cfg, escalated=escalated)
        assert d1.decision == Decision.ESCALATE
        assert "crash loop" in d1.reason
        # Second call should return NONE (already escalated)
        d2 = decide(w, "$ ", config=cfg, escalated=escalated)
        assert d2.decision == Decision.NONE
        assert "already escalated" in d2.reason
        # Third call — still NONE (no spam)
        d3 = decide(w, "$ ", config=cfg, escalated=escalated)
        assert d3.decision == Decision.NONE

    def test_revive_count_resets_on_buzzing(self):
        w = _make_worker(state=WorkerState.STUNG)
        w.revive_count = 2
        # Transition to BUZZING resets count
        w.update_state(WorkerState.BUZZING)
        assert w.revive_count == 0

    def test_revive_grace_blocks_stung(self):
        """After revive, STUNG readings are ignored for the grace period."""
        w = _make_worker(state=WorkerState.BUZZING)
        w.record_revive()  # sets _revive_at to now
        # Poll detects shell (STUNG) right after revive — should be ignored
        changed = w.update_state(WorkerState.STUNG)
        assert not changed
        assert w.state == WorkerState.BUZZING

    def test_revive_grace_expires(self):
        """After the grace period, STUNG readings are accepted again."""
        w = _make_worker(state=WorkerState.BUZZING)
        w.record_revive()
        # Simulate grace period expiring
        w._revive_at -= w.revive_grace + 1
        w.update_state(WorkerState.STUNG)  # first — debounced
        changed = w.update_state(WorkerState.STUNG)  # second — accepted
        assert changed
        assert w.state == WorkerState.STUNG

    def test_revive_grace_allows_non_stung(self):
        """Grace period only blocks STUNG — other transitions still work."""
        w = _make_worker(state=WorkerState.BUZZING)
        w.record_revive()
        # RESTING requires 3 confirmations, so first two return False
        w.update_state(WorkerState.RESTING)
        w.update_state(WorkerState.RESTING)
        changed = w.update_state(WorkerState.RESTING)
        assert changed
        assert w.state == WorkerState.RESTING


class TestDecideWithConfig:
    def test_custom_escalation_threshold(self, escalated):
        cfg = DroneConfig(escalation_threshold=60.0)
        w = _make_worker(
            state=WorkerState.WAITING,
            resting_since=time.time() - 20,
        )
        d = decide(w, "some unknown content", config=cfg, escalated=escalated)
        # 20s < 60s threshold, should NOT escalate
        assert d.decision == Decision.NONE

    def test_low_escalation_threshold(self, escalated):
        cfg = DroneConfig(escalation_threshold=2.0)
        w = _make_worker(
            state=WorkerState.WAITING,
            resting_since=time.time() - 5,
        )
        d = decide(w, "some unknown content", config=cfg, escalated=escalated)
        assert d.decision == Decision.ESCALATE


class TestApprovalRules:
    """Approval rules on choice menu prompts."""

    def _choice_content(self, selected: str = "Always allow") -> str:
        return f"""> 1. {selected}
  2. Yes
  3. No
Enter to select · ↑/↓ to navigate"""

    def test_approve_rule_matches(self, escalated):
        from swarm.config import DroneApprovalRule

        cfg = DroneConfig(approval_rules=[DroneApprovalRule("Always allow", "approve")])
        w = _make_worker(state=WorkerState.WAITING)
        d = decide(w, self._choice_content(), config=cfg, escalated=escalated)
        assert d.decision == Decision.CONTINUE

    def test_escalate_rule_matches(self, escalated):
        from swarm.config import DroneApprovalRule

        cfg = DroneConfig(approval_rules=[DroneApprovalRule("delete|remove", "escalate")])
        w = _make_worker(state=WorkerState.WAITING)
        content = self._choice_content("delete old files")
        d = decide(w, content, config=cfg, escalated=escalated)
        assert d.decision == Decision.ESCALATE
        assert "choice requires approval" in d.reason

    def test_first_match_wins(self, escalated):
        from swarm.config import DroneApprovalRule

        cfg = DroneConfig(
            approval_rules=[
                DroneApprovalRule("Always", "approve"),
                DroneApprovalRule("Always", "escalate"),
            ]
        )
        w = _make_worker(state=WorkerState.WAITING)
        d = decide(w, self._choice_content(), config=cfg, escalated=escalated)
        assert d.decision == Decision.CONTINUE  # first rule wins

    def test_no_rules_legacy_continue(self, escalated):
        cfg = DroneConfig(approval_rules=[])
        w = _make_worker(state=WorkerState.WAITING)
        d = decide(w, self._choice_content(), config=cfg, escalated=escalated)
        assert d.decision == Decision.CONTINUE

    def test_case_insensitive(self, escalated):
        from swarm.config import DroneApprovalRule

        cfg = DroneConfig(approval_rules=[DroneApprovalRule("always allow", "escalate")])
        w = _make_worker(state=WorkerState.WAITING)
        d = decide(w, self._choice_content("Always Allow"), config=cfg, escalated=escalated)
        assert d.decision == Decision.ESCALATE

    def test_escalate_rule_only_fires_once(self, escalated):
        """Choice-menu escalation should not spam — escalate once, then NONE."""
        from swarm.config import DroneApprovalRule

        cfg = DroneConfig(approval_rules=[DroneApprovalRule("delete", "escalate")])
        w = _make_worker(state=WorkerState.WAITING)
        content = self._choice_content("delete old files")
        d1 = decide(w, content, config=cfg, escalated=escalated)
        assert d1.decision == Decision.ESCALATE
        d2 = decide(w, content, config=cfg, escalated=escalated)
        assert d2.decision == Decision.NONE
        assert "already escalated" in d2.reason


class TestPlanEscalation:
    """Plan approval prompts always escalate — never auto-approve."""

    def _plan_content(self) -> str:
        return """Here is my plan for implementing the feature:

## Plan
1. Create the new module
2. Add tests
3. Update docs

Do you want me to proceed with this plan?
> 1. Yes, proceed
  2. No, revise
  3. Cancel
Enter to select"""

    def test_plan_prompt_always_escalates(self, escalated):
        w = _make_worker(state=WorkerState.WAITING)
        d = decide(w, self._plan_content(), escalated=escalated)
        assert d.decision == Decision.ESCALATE
        assert "plan" in d.reason.lower()

    def test_plan_escalation_only_fires_once(self, escalated):
        """Plan escalation should not spam — escalate once, then NONE until worker resumes."""
        w = _make_worker(state=WorkerState.WAITING)
        d1 = decide(w, self._plan_content(), escalated=escalated)
        assert d1.decision == Decision.ESCALATE
        d2 = decide(w, self._plan_content(), escalated=escalated)
        assert d2.decision == Decision.NONE
        assert "already escalated" in d2.reason

    def test_plan_escalation_resets_after_buzzing(self, escalated):
        """After worker goes back to BUZZING, next plan prompt re-escalates."""
        w = _make_worker(state=WorkerState.WAITING)
        d1 = decide(w, self._plan_content(), escalated=escalated)
        assert d1.decision == Decision.ESCALATE
        # Worker resumes working — BUZZING clears the escalated set
        w.state = WorkerState.BUZZING
        decide(w, "esc to interrupt", escalated=escalated)
        # New plan prompt → should escalate again
        w.state = WorkerState.WAITING
        d2 = decide(w, self._plan_content(), escalated=escalated)
        assert d2.decision == Decision.ESCALATE

    def test_plan_escalates_even_with_approve_rules(self, escalated):
        from swarm.config import DroneApprovalRule

        cfg = DroneConfig(approval_rules=[DroneApprovalRule(".*", "approve")])
        w = _make_worker(state=WorkerState.WAITING)
        d = decide(w, self._plan_content(), config=cfg, escalated=escalated)
        assert d.decision == Decision.ESCALATE

    def test_non_plan_choice_not_affected(self, escalated):
        w = _make_worker(state=WorkerState.WAITING)
        content = """> 1. Yes
  2. No
Enter to select"""
        d = decide(w, content, escalated=escalated)
        assert d.decision == Decision.CONTINUE

    def test_plan_word_in_conversation_does_not_trigger_plan_escalation(self, escalated):
        """Regression: 'plan' in worker output should not cause plan escalation.

        When a worker implementing a plan shows a permission prompt (e.g., grep),
        the drone should treat it as a regular choice, not a plan approval prompt.
        The escalation reason should NOT contain 'plan requires user approval'.
        """
        w = _make_worker(state=WorkerState.WAITING)
        content = """Phase 1 of the plan is complete.
The plan was already approved by the operator.
Now executing the approved plan for type safety fixes.

Grep command
  grep -r "list\\[dict\\]" src/
> 1. Allow
  2. Allow always
  3. Deny
Enter to select"""
        d = decide(w, content, escalated=escalated)
        # Should be treated as a regular choice, NOT a plan escalation
        assert d.decision == Decision.CONTINUE
        assert "plan requires" not in d.reason.lower()


class TestSafetyPatterns:
    """Built-in safety patterns escalate destructive operations."""

    def test_drop_table_escalates(self, escalated):
        from swarm.config import DroneApprovalRule

        cfg = DroneConfig(approval_rules=[DroneApprovalRule("Bash", "approve")])
        w = _make_worker(state=WorkerState.WAITING)
        content = """Bash command
  psql -c "DROP TABLE users;"
Do you want to proceed?
> 1. Yes
  2. Yes, and don't ask again
  3. No
Esc to cancel"""
        d = decide(w, content, config=cfg, escalated=escalated)
        assert d.decision == Decision.ESCALATE

    def test_truncate_escalates(self, escalated):
        from swarm.config import DroneApprovalRule

        cfg = DroneConfig(approval_rules=[DroneApprovalRule("Bash", "approve")])
        w = _make_worker(state=WorkerState.WAITING)
        content = """Bash command
  psql -c "TRUNCATE nexus_call_log;"
Do you want to proceed?
> 1. Yes
  2. No
Esc to cancel"""
        d = decide(w, content, config=cfg, escalated=escalated)
        assert d.decision == Decision.ESCALATE

    def test_safe_select_on_production_db_approves(self, escalated):
        """SELECT queries on production databases should NOT be blocked."""
        from swarm.config import DroneApprovalRule

        cfg = DroneConfig(approval_rules=[DroneApprovalRule("Bash", "approve")])
        w = _make_worker(state=WorkerState.WAITING)
        content = """Bash command
  PGPASSWORD='secret' psql -h db.postgres.database.azure.com \
  -U admin -d v6_production -c "
  SELECT id, \"stagingRecordId\", \"callType\"
  FROM nexus_call_log
  WHERE \"stagingRecordId\" = 11525;
  " 2>&1
  Query production DB for call logs

Do you want to proceed?
> 1. Yes
  2. Yes, and don't ask again for PGPASSWORD psql commands
  3. No
Esc to cancel"""
        d = decide(w, content, config=cfg, escalated=escalated)
        assert d.decision == Decision.CONTINUE

    def test_read_uploads_approves(self, escalated):
        """Read from swarm uploads should be approved by tool-name rules."""
        from swarm.config import DroneApprovalRule

        cfg = DroneConfig(approval_rules=[DroneApprovalRule("Read", "approve")])
        w = _make_worker(state=WorkerState.WAITING)
        content = """Read file
  Read(~/.swarm/uploads/09b31b4bcc13_image.png)
Do you want to proceed?
> 1. Yes
  2. Yes, allow reading from uploads/ during this session
  3. No
Esc to cancel"""
        d = decide(w, content, config=cfg, escalated=escalated)
        assert d.decision == Decision.CONTINUE

    def test_allowed_read_path_approves_without_rules(self, escalated):
        """allowed_read_paths auto-approves Read from configured dirs."""
        cfg = DroneConfig(allowed_read_paths=["~/.swarm/uploads/"])
        w = _make_worker(state=WorkerState.WAITING)
        content = """Read file
  Read(~/.swarm/uploads/09b31b4bcc13_image.png)
Do you want to proceed?
> 1. Yes
  2. No
Esc to cancel"""
        d = decide(w, content, config=cfg, escalated=escalated)
        assert d.decision == Decision.CONTINUE
        assert "allowed path" in d.reason

    def test_allowed_read_path_other_dirs_uses_safe_pattern(self, escalated):
        """Read from non-allowed dirs still approved via safe patterns."""
        cfg = DroneConfig(allowed_read_paths=["~/.swarm/uploads/"])
        w = _make_worker(state=WorkerState.WAITING)
        content = """Read file
  Read(/etc/passwd)
Do you want to proceed?
> 1. Yes
  2. No
Esc to cancel"""
        d = decide(w, content, config=cfg, escalated=escalated)
        assert d.decision == Decision.CONTINUE
        assert "safe operation" in d.reason

    def test_allowed_read_path_with_absolute_path(self, escalated):
        """allowed_read_paths works with absolute paths too."""
        cfg = DroneConfig(allowed_read_paths=["/home/bschleifer/.swarm/uploads/"])
        w = _make_worker(state=WorkerState.WAITING)
        content = """Read file
  Read(/home/bschleifer/.swarm/uploads/file.txt)
Do you want to proceed?
> 1. Yes
  2. No
Esc to cancel"""
        d = decide(w, content, config=cfg, escalated=escalated)
        assert d.decision == Decision.CONTINUE
        assert "allowed path" in d.reason

    def test_allowed_read_path_traversal_uses_safe_pattern(self, escalated):
        """Path traversal via ../ doesn't match allowed_read_paths but Read is safe."""
        cfg = DroneConfig(allowed_read_paths=["~/.swarm/uploads/"])
        w = _make_worker(state=WorkerState.WAITING)
        content = """Read file
  Read(~/.swarm/uploads/../../../etc/passwd)
Do you want to proceed?
> 1. Yes
  2. No
Esc to cancel"""
        d = decide(w, content, config=cfg, escalated=escalated)
        # Read is inherently non-destructive — approved via safe patterns
        assert d.decision == Decision.CONTINUE
        assert "safe operation" in d.reason

    def test_allowed_read_path_no_prefix_false_positive(self, escalated):
        """uploads/ must not match uploads_evil/ — but Read is still safe."""
        cfg = DroneConfig(allowed_read_paths=["~/.swarm/uploads"])
        w = _make_worker(state=WorkerState.WAITING)
        content = """Read file
  Read(~/.swarm/uploads_evil/secret.txt)
Do you want to proceed?
> 1. Yes
  2. No
Esc to cancel"""
        d = decide(w, content, config=cfg, escalated=escalated)
        # Doesn't match allowed_read_paths (prefix attack) but Read is
        # inherently non-destructive — approved via safe patterns
        assert d.decision == Decision.CONTINUE
        assert "safe operation" in d.reason

    def test_allowed_read_uses_last_match(self, escalated):
        """When scrollback has multiple Read()s, only the last one matters."""
        cfg = DroneConfig(allowed_read_paths=["~/.swarm/uploads/"])
        w = _make_worker(state=WorkerState.WAITING)
        # Older Read from a non-allowed path is higher in scrollback;
        # the CURRENT prompt is a Read from uploads — should approve.
        content = """Read file
  Read(/home/user/projects/secret.py)
Do you want to proceed?
> 1. Yes
  2. No
Esc to cancel

... more output ...

Read file
  Read(~/.swarm/uploads/screenshot.png)
Do you want to proceed?
> 1. Yes
  2. No
Esc to cancel"""
        d = decide(w, content, config=cfg, escalated=escalated)
        assert d.decision == Decision.CONTINUE
        assert "allowed path" in d.reason

    def test_allowed_read_old_uploads_does_not_shadow(self, escalated):
        """An old Read from uploads shouldn't match allowed_read_paths for a new Read elsewhere."""
        cfg = DroneConfig(allowed_read_paths=["~/.swarm/uploads/"])
        w = _make_worker(state=WorkerState.WAITING)
        # Old Read from uploads higher in scrollback, current Read from /etc/passwd
        content = """Read file
  Read(~/.swarm/uploads/old_image.png)
Do you want to proceed?
> 1. Yes

... more output ...

Read file
  Read(/etc/passwd)
Do you want to proceed?
> 1. Yes
  2. No
Esc to cancel"""
        d = decide(w, content, config=cfg, escalated=escalated)
        # Last Read path doesn't match allowed_read_paths, but Read is
        # inherently non-destructive — approved via safe patterns
        assert d.decision == Decision.CONTINUE
        assert "safe operation" in d.reason

    def test_caret_anchor_matches_line_start_multiline(self, escalated):
        """Rules with ^ should match start-of-line, not just start-of-string."""
        from swarm.config import DroneApprovalRule

        cfg = DroneConfig(approval_rules=[DroneApprovalRule("^Do you want to proceed", "approve")])
        w = _make_worker(state=WorkerState.WAITING)
        # "Do you want to proceed" is NOT at position 0 of the string
        content = """Bash command
  git commit -m "fix typo"
Do you want to proceed?
> 1. Yes
  2. No
Esc to cancel"""
        d = decide(w, content, config=cfg, escalated=escalated)
        assert d.decision == Decision.CONTINUE

    def test_rules_match_full_content_not_just_summary(self, escalated):
        """Approval rules must see the full worker output, not just the choice summary."""
        from swarm.config import DroneApprovalRule

        cfg = DroneConfig(approval_rules=[DroneApprovalRule("psql", "approve")])
        w = _make_worker(state=WorkerState.WAITING)
        content = """Bash command
  psql -c "SELECT 1;"
Do you want to proceed?
> 1. Yes
  2. No
Esc to cancel"""
        d = decide(w, content, config=cfg, escalated=escalated)
        assert d.decision == Decision.CONTINUE


class TestBuiltinSafePatterns:
    """Built-in safe patterns auto-approve read-only operations."""

    def test_bash_ls_approves(self, escalated):
        """Bash ls command should be auto-approved as safe operation."""
        w = _make_worker(state=WorkerState.WAITING)
        content = """Bash command
  Bash(ls /tmp/swarm*)
Do you want to proceed?
> 1. Yes
  2. No
Esc to cancel"""
        d = decide(w, content, escalated=escalated)
        assert d.decision == Decision.CONTINUE
        assert "safe operation" in d.reason

    def test_read_tool_is_safe_pattern(self, escalated):
        """Read tool should be auto-approved as safe operation (inherently non-destructive)."""
        w = _make_worker(state=WorkerState.WAITING)
        content = """Read file
  Read(/home/user/projects/readme.md)
Do you want to proceed?
> 1. Yes
  2. No
Esc to cancel"""
        d = decide(w, content, escalated=escalated)
        assert d.decision == Decision.CONTINUE
        assert "safe operation" in d.reason

    def test_glob_tool_approves(self, escalated):
        """Glob tool prompt should be auto-approved as safe operation."""
        w = _make_worker(state=WorkerState.WAITING)
        content = """Glob pattern
  Glob(src/**/*.py)
Do you want to proceed?
> 1. Yes
  2. No
Esc to cancel"""
        d = decide(w, content, escalated=escalated)
        assert d.decision == Decision.CONTINUE
        assert "safe operation" in d.reason

    def test_grep_tool_approves(self, escalated):
        """Grep tool prompt should be auto-approved as safe operation."""
        w = _make_worker(state=WorkerState.WAITING)
        content = """Search content
  Grep(error)
Do you want to proceed?
> 1. Yes
  2. No
Esc to cancel"""
        d = decide(w, content, escalated=escalated)
        assert d.decision == Decision.CONTINUE
        assert "safe operation" in d.reason

    def test_bash_cat_approves(self, escalated):
        """Bash cat (read-only) should be auto-approved."""
        w = _make_worker(state=WorkerState.WAITING)
        content = """Bash command
  Bash(cat /tmp/report.txt)
Do you want to proceed?
> 1. Yes
  2. No
Esc to cancel"""
        d = decide(w, content, escalated=escalated)
        assert d.decision == Decision.CONTINUE
        assert "safe operation" in d.reason

    def test_safe_pattern_blocked_by_destructive(self, escalated):
        """Safe pattern should NOT override destructive safety patterns."""
        from swarm.config import DroneApprovalRule

        cfg = DroneConfig(approval_rules=[DroneApprovalRule("Bash", "approve")])
        w = _make_worker(state=WorkerState.WAITING)
        content = """Bash command
  Bash(rm -rf /tmp/important)
Do you want to proceed?
> 1. Yes
  2. No
Esc to cancel"""
        d = decide(w, content, config=cfg, escalated=escalated)
        assert d.decision == Decision.ESCALATE

    def test_user_question_not_affected_by_safe_patterns(self, escalated):
        """User questions should still escalate even with safe-looking content."""
        w = _make_worker(state=WorkerState.WAITING)
        content = """\
Which file should I Read?
> 1. Read(src/main.py)
  2. Read(src/utils.py)
  3. Type something.
Enter to select · Esc to cancel"""
        d = decide(w, content, escalated=escalated)
        assert d.decision == Decision.ESCALATE
        assert "user question" in d.reason

    def test_git_status_approves(self, escalated):
        """git status should be auto-approved as safe read-only operation."""
        w = _make_worker(state=WorkerState.WAITING)
        content = """Bash command
  Bash(git status)
Do you want to proceed?
> 1. Yes
  2. No
Esc to cancel"""
        d = decide(w, content, escalated=escalated)
        assert d.decision == Decision.CONTINUE
        assert "safe operation" in d.reason

    def test_git_log_approves(self, escalated):
        """git log should be auto-approved as safe read-only operation."""
        w = _make_worker(state=WorkerState.WAITING)
        content = """Bash command
  Bash(git log --oneline -10)
Do you want to proceed?
> 1. Yes
  2. No
Esc to cancel"""
        d = decide(w, content, escalated=escalated)
        assert d.decision == Decision.CONTINUE
        assert "safe operation" in d.reason

    def test_git_diff_approves(self, escalated):
        """git diff should be auto-approved as safe read-only operation."""
        w = _make_worker(state=WorkerState.WAITING)
        content = """Bash command
  Bash(git diff HEAD~1)
Do you want to proceed?
> 1. Yes
  2. No
Esc to cancel"""
        d = decide(w, content, escalated=escalated)
        assert d.decision == Decision.CONTINUE
        assert "safe operation" in d.reason

    def test_git_show_approves(self, escalated):
        """git show should be auto-approved as safe read-only operation."""
        w = _make_worker(state=WorkerState.WAITING)
        content = """Bash command
  Bash(git show HEAD:src/main.py)
Do you want to proceed?
> 1. Yes
  2. No
Esc to cancel"""
        d = decide(w, content, escalated=escalated)
        assert d.decision == Decision.CONTINUE
        assert "safe operation" in d.reason

    def test_git_branch_approves(self, escalated):
        """git branch (list) should be auto-approved as safe read-only operation."""
        w = _make_worker(state=WorkerState.WAITING)
        content = """Bash command
  Bash(git branch -a)
Do you want to proceed?
> 1. Yes
  2. No
Esc to cancel"""
        d = decide(w, content, escalated=escalated)
        assert d.decision == Decision.CONTINUE
        assert "safe operation" in d.reason

    def test_uv_run_pytest_approves(self, escalated):
        """uv run pytest should be auto-approved as safe operation."""
        w = _make_worker(state=WorkerState.WAITING)
        content = """Bash command
  Bash(uv run pytest tests/ -q)
Do you want to proceed?
> 1. Yes
  2. No
Esc to cancel"""
        d = decide(w, content, escalated=escalated)
        assert d.decision == Decision.CONTINUE
        assert "safe operation" in d.reason

    def test_uv_run_ruff_approves(self, escalated):
        """uv run ruff should be auto-approved as safe operation."""
        w = _make_worker(state=WorkerState.WAITING)
        content = """Bash command
  Bash(uv run ruff check src/)
Do you want to proceed?
> 1. Yes
  2. No
Esc to cancel"""
        d = decide(w, content, escalated=escalated)
        assert d.decision == Decision.CONTINUE
        assert "safe operation" in d.reason

    def test_read_tool_approves(self, escalated):
        """Read tool prompt should be auto-approved as safe operation."""
        w = _make_worker(state=WorkerState.WAITING)
        content = """Read file
  Read(/home/user/projects/readme.md)
Do you want to proceed?
> 1. Yes
  2. No
Esc to cancel"""
        d = decide(w, content, escalated=escalated)
        assert d.decision == Decision.CONTINUE
        assert "safe operation" in d.reason


class TestPushToDefaultBranchUserConfigurable:
    """Direct push to main/master is user-configurable, not hardcoded in ALWAYS_ESCALATE.

    The `git push <remote> (main|master)` pattern was removed from the
    safety net (rules.py) so it can be opted-into per-repo via
    drones.approval_rules. Repos with PR-only workflows add an `escalate`
    rule; repos where direct-to-main is the legitimate workflow (personal
    IaC, single-maintainer projects) don't need the workaround.
    """

    def test_main_push_falls_through_to_user_rules(self, escalated):
        """With a user-configured Bash-approve rule, push-to-main approves."""
        from swarm.config import DroneApprovalRule

        cfg = DroneConfig(approval_rules=[DroneApprovalRule("Bash", "approve")])
        w = _make_worker(state=WorkerState.WAITING)
        content = """Bash command
  git push origin main
Do you want to proceed?
> 1. Yes
  2. No
Esc to cancel"""
        d = decide(w, content, config=cfg, escalated=escalated)
        assert d.decision == Decision.CONTINUE

    def test_main_push_can_still_escalate_via_user_rule(self, escalated):
        """Repos that want PR-only enforcement add an explicit escalate rule."""
        from swarm.config import DroneApprovalRule

        cfg = DroneConfig(
            approval_rules=[
                DroneApprovalRule(r"git\s+push\s+\S+\s+(main|master)\b", "escalate"),
                DroneApprovalRule("Bash", "approve"),
            ]
        )
        w = _make_worker(state=WorkerState.WAITING)
        content = """Bash command
  git push origin main
Do you want to proceed?
> 1. Yes
  2. No
Esc to cancel"""
        d = decide(w, content, config=cfg, escalated=escalated)
        assert d.decision == Decision.ESCALATE

    def test_feature_branch_push_approves(self, escalated):
        from swarm.config import DroneApprovalRule

        cfg = DroneConfig(approval_rules=[DroneApprovalRule("Bash", "approve")])
        w = _make_worker(state=WorkerState.WAITING)
        content = """Bash command
  git push origin feature/my-branch
Do you want to proceed?
> 1. Yes
  2. No
Esc to cancel"""
        d = decide(w, content, config=cfg, escalated=escalated)
        assert d.decision == Decision.CONTINUE


class TestScrollbackTrimming:
    """Approval rules and safety patterns only see the last 30 lines."""

    def test_stale_plan_text_does_not_escalate_bash_prompt(self, escalated):
        """Regression: 'plan' in old scrollback should not trigger plan escalation
        on a fresh Bash permission prompt lower in the output."""
        from swarm.config import DroneApprovalRule

        cfg = DroneConfig(approval_rules=[DroneApprovalRule("Bash", "approve")])
        w = _make_worker(state=WorkerState.WAITING)
        # Build content: old scrollback with "plan" text + 40 blank lines + Bash prompt
        old_scrollback = "Here is the plan for implementing the feature.\n" * 5
        padding = "\n" * 40
        bash_prompt = """Bash command
  uv run ruff check src/
Do you want to proceed?
> 1. Yes
  2. No
Esc to cancel"""
        content = old_scrollback + padding + bash_prompt
        d = decide(w, content, config=cfg, escalated=escalated)
        # The Bash prompt should be approved via the "Bash" approval rule,
        # NOT escalated because of the old "plan" text in scrollback.
        assert d.decision == Decision.CONTINUE

    def test_stale_destructive_text_does_not_escalate_safe_command(self, escalated):
        """Old 'rm -rf' in scrollback should not block a safe 'ls' command."""
        w = _make_worker(state=WorkerState.WAITING)
        old_scrollback = "Previously ran: rm -rf /tmp/old\n" * 3
        padding = "\n" * 40
        safe_prompt = """Bash command
  Bash(ls /tmp/new)
Do you want to proceed?
> 1. Yes
  2. No
Esc to cancel"""
        content = old_scrollback + padding + safe_prompt
        d = decide(w, content, escalated=escalated)
        assert d.decision == Decision.CONTINUE
        assert "safe operation" in d.reason

    def test_recent_destructive_text_still_escalates(self, escalated):
        """Destructive text within the last 30 lines should still escalate."""
        from swarm.config import DroneApprovalRule

        cfg = DroneConfig(approval_rules=[DroneApprovalRule("Bash", "approve")])
        w = _make_worker(state=WorkerState.WAITING)
        content = """Bash command
  rm -rf /var/data
Do you want to proceed?
> 1. Yes
  2. No
Esc to cancel"""
        d = decide(w, content, config=cfg, escalated=escalated)
        assert d.decision == Decision.ESCALATE

    def test_plan_text_in_rule_area_still_escalates(self, escalated):
        """'plan' within 15 lines of the prompt should still trigger an escalation rule."""
        from swarm.config import DroneApprovalRule

        cfg = DroneConfig(
            approval_rules=[
                DroneApprovalRule(r"\bplan\b", "escalate"),
                DroneApprovalRule("Bash", "approve"),
            ]
        )
        w = _make_worker(state=WorkerState.WAITING)
        # "plan" only 5 lines above the prompt — within 15-line rule window
        content = """proceed with the plan now

Bash command
  npm install
Do you want to proceed?
> 1. Yes
  2. No
Esc to cancel"""
        d = decide(w, content, config=cfg, escalated=escalated)
        assert d.decision == Decision.ESCALATE

    def test_plan_text_20_lines_above_prompt_does_not_trigger_rule(self, escalated):
        """Regression: 'plan' 20+ lines above the prompt should NOT trigger a \\bplan\\b rule.

        The rule window is narrower (15 lines) than the safe-pattern window (30 lines),
        so stale context text doesn't false-positive on user rules.
        """
        from swarm.config import DroneApprovalRule

        cfg = DroneConfig(
            approval_rules=[
                DroneApprovalRule(r"\bplan\b", "escalate"),
                DroneApprovalRule("Bash", "approve"),
            ]
        )
        w = _make_worker(state=WorkerState.WAITING)
        # "plan" text separated by 20 lines of other output from the prompt
        plan_text = "Here is the plan to address the gaps in the codebase.\n"
        filler = "Working on implementation...\n" * 20
        bash_prompt = """Bash command
  ls -la ~/projects/swarm/src/
  List files in directory

Do you want to proceed?
> 1. Yes
  2. Yes, and don't ask again
  3. No
Esc to cancel"""
        content = plan_text + filler + bash_prompt
        d = decide(w, content, config=cfg, escalated=escalated)
        # "plan" is beyond the 15-line rule window, and "ls" matches safe patterns
        assert d.decision == Decision.CONTINUE


class TestNewFormatSafePatterns:
    """Safe patterns for Claude Code's current prompt format (no Bash() wrapper)."""

    def test_new_format_ls_approves(self, escalated):
        """Regression: 'Bash command\\n  ls ...' without Bash() wrapper should auto-approve."""
        w = _make_worker(state=WorkerState.WAITING)
        content = """Bash command
  ls -la ~/projects/swarm/src/
  List files in directory

Do you want to proceed?
> 1. Yes
  2. Yes, and don't ask again
  3. No
Esc to cancel"""
        d = decide(w, content, escalated=escalated)
        assert d.decision == Decision.CONTINUE
        assert "safe operation" in d.reason

    def test_new_format_git_status_approves(self, escalated):
        """'Bash command\\n  git status' should auto-approve."""
        w = _make_worker(state=WorkerState.WAITING)
        content = """Bash command
  git status
  Show working tree status

Do you want to proceed?
> 1. Yes
  2. No
Esc to cancel"""
        d = decide(w, content, escalated=escalated)
        assert d.decision == Decision.CONTINUE
        assert "safe operation" in d.reason

    def test_new_format_git_diff_approves(self, escalated):
        """'Bash command\\n  git diff ...' should auto-approve."""
        w = _make_worker(state=WorkerState.WAITING)
        content = """Bash command
  git diff HEAD~3
  Show recent changes

Do you want to proceed?
> 1. Yes
  2. No
Esc to cancel"""
        d = decide(w, content, escalated=escalated)
        assert d.decision == Decision.CONTINUE
        assert "safe operation" in d.reason

    def test_new_format_uv_run_pytest_approves(self, escalated):
        """'Bash command\\n  uv run pytest ...' should auto-approve."""
        w = _make_worker(state=WorkerState.WAITING)
        content = """Bash command
  uv run pytest tests/ -q
  Run test suite

Do you want to proceed?
> 1. Yes
  2. No
Esc to cancel"""
        d = decide(w, content, escalated=escalated)
        assert d.decision == Decision.CONTINUE
        assert "safe operation" in d.reason

    def test_new_format_uv_run_ruff_approves(self, escalated):
        """'Bash command\\n  uv run ruff format src/' should auto-approve."""
        w = _make_worker(state=WorkerState.WAITING)
        content = """Bash command
  uv run ruff format src/ tests/
  Format code

Do you want to proceed?
> 1. Yes
  2. No
Esc to cancel"""
        d = decide(w, content, escalated=escalated)
        assert d.decision == Decision.CONTINUE
        assert "safe operation" in d.reason

    def test_new_format_find_approves(self, escalated):
        """'Bash command\\n  find ...' should auto-approve."""
        w = _make_worker(state=WorkerState.WAITING)
        content = """Bash command
  find src/ -name "*.py" -type f
  Find Python files

Do you want to proceed?
> 1. Yes
  2. No
Esc to cancel"""
        d = decide(w, content, escalated=escalated)
        assert d.decision == Decision.CONTINUE
        assert "safe operation" in d.reason

    def test_new_format_cat_approves(self, escalated):
        """'Bash command\\n  cat ...' should auto-approve."""
        w = _make_worker(state=WorkerState.WAITING)
        content = """Bash command
  cat /tmp/output.log
  Read file contents

Do you want to proceed?
> 1. Yes
  2. No
Esc to cancel"""
        d = decide(w, content, escalated=escalated)
        assert d.decision == Decision.CONTINUE
        assert "safe operation" in d.reason

    def test_new_format_read_file_approves(self, escalated):
        """'Read file\\n  ...' (new format header) should auto-approve."""
        w = _make_worker(state=WorkerState.WAITING)
        content = """Read file
  /home/user/projects/swarm/src/main.py

Do you want to proceed?
> 1. Yes
  2. No
Esc to cancel"""
        d = decide(w, content, escalated=escalated)
        assert d.decision == Decision.CONTINUE
        assert "safe operation" in d.reason

    def test_new_format_glob_header_approves(self, escalated):
        """'Glob pattern' (new format) should auto-approve."""
        w = _make_worker(state=WorkerState.WAITING)
        content = """Glob pattern
  src/**/*.py

Do you want to proceed?
> 1. Yes
  2. No
Esc to cancel"""
        d = decide(w, content, escalated=escalated)
        assert d.decision == Decision.CONTINUE
        assert "safe operation" in d.reason

    def test_new_format_grep_header_approves(self, escalated):
        """'Grep content' (new format) should auto-approve."""
        w = _make_worker(state=WorkerState.WAITING)
        content = """Grep content
  pattern: "import asyncio"

Do you want to proceed?
> 1. Yes
  2. No
Esc to cancel"""
        d = decide(w, content, escalated=escalated)
        assert d.decision == Decision.CONTINUE
        assert "safe operation" in d.reason

    def test_new_format_websearch_approves(self, escalated):
        """'WebSearch query' (new format) should auto-approve."""
        w = _make_worker(state=WorkerState.WAITING)
        content = """WebSearch query
  "python asyncio best practices"

Do you want to proceed?
> 1. Yes
  2. No
Esc to cancel"""
        d = decide(w, content, escalated=escalated)
        assert d.decision == Decision.CONTINUE
        assert "safe operation" in d.reason

    def test_new_format_rm_does_not_approve(self, escalated):
        """'Bash command\\n  rm ...' should NOT be auto-approved."""
        from swarm.config import DroneApprovalRule

        cfg = DroneConfig(approval_rules=[DroneApprovalRule("Bash", "approve")])
        w = _make_worker(state=WorkerState.WAITING)
        content = """Bash command
  rm -rf /tmp/important
  Delete directory

Do you want to proceed?
> 1. Yes
  2. No
Esc to cancel"""
        d = decide(w, content, config=cfg, escalated=escalated)
        assert d.decision == Decision.ESCALATE

    def test_new_format_npm_install_not_safe(self, escalated):
        """'Bash command\\n  npm install' is not a safe command — uses approval rules."""
        from swarm.config import DroneApprovalRule

        cfg = DroneConfig(approval_rules=[DroneApprovalRule("npm", "approve")])
        w = _make_worker(state=WorkerState.WAITING)
        content = """Bash command
  npm install express
  Install package

Do you want to proceed?
> 1. Yes
  2. No
Esc to cancel"""
        d = decide(w, content, config=cfg, escalated=escalated)
        # Not a safe builtin — approved via the "npm" approval rule
        assert d.decision == Decision.CONTINUE
        assert d.source == "rule"


class TestEventBasedDecisions:
    """Verify that events parameter flows through decide() and affects decisions."""

    def test_events_passed_to_drone_decision(self, escalated):
        """DroneDecision should carry events when provided."""
        from swarm.providers.events import EventType, TerminalEvent

        events = [TerminalEvent(EventType.TOOL_CALL, tool_name="Read")]
        w = _make_worker(state=WorkerState.WAITING)
        content = """\
Read file
  /home/user/project/main.py

Do you want to proceed?
> 1. Yes
  2. No
Esc to cancel"""
        d = decide(w, content, escalated=escalated, events=events)
        assert d.events is events

    def test_safe_tool_event_auto_approves(self, escalated):
        """A TOOL_CALL event with a safe tool_name should auto-approve."""
        from swarm.providers.events import EventType, TerminalEvent

        events = [
            TerminalEvent(EventType.CHOICE),
            TerminalEvent(EventType.TOOL_CALL, tool_name="Glob"),
        ]
        w = _make_worker(state=WorkerState.WAITING)
        content = """\
Glob pattern
  src/**/*.py
> 1. Yes
  2. No
Esc to cancel"""
        d = decide(w, content, escalated=escalated, events=events)
        assert d.decision == Decision.CONTINUE
        assert "safe operation" in d.reason

    def test_unsafe_tool_event_does_not_auto_approve(self, escalated):
        """A TOOL_CALL event with a non-safe tool should NOT auto-approve via event."""
        from swarm.config import DroneApprovalRule
        from swarm.providers.events import EventType, TerminalEvent

        cfg = DroneConfig(approval_rules=[DroneApprovalRule("Edit", "approve")])
        events = [
            TerminalEvent(EventType.CHOICE),
            TerminalEvent(EventType.TOOL_CALL, tool_name="Edit"),
        ]
        w = _make_worker(state=WorkerState.WAITING)
        content = """\
Edit file
  src/main.py
> 1. Yes
  2. No
Esc to cancel"""
        d = decide(w, content, config=cfg, escalated=escalated, events=events)
        # Should be approved by user rule, not by safe-tool builtin
        assert d.decision == Decision.CONTINUE
        assert d.source == "rule"

    def test_user_question_event_escalates(self, escalated):
        """USER_QUESTION event should trigger escalation."""
        from swarm.providers.events import EventType, TerminalEvent

        events = [
            TerminalEvent(EventType.USER_QUESTION),
            TerminalEvent(EventType.CHOICE),
        ]
        w = _make_worker(state=WorkerState.WAITING)
        content = """\
Which approach do you prefer?
> 1. Option A
  2. Option B
  3. Type something.
Esc to cancel"""
        d = decide(w, content, escalated=escalated, events=events)
        assert d.decision == Decision.ESCALATE
        assert "user question" in d.reason

    def test_accept_edits_event_with_bash_escalates(self, escalated):
        """ACCEPT_EDITS event with has_bash=True should escalate."""
        from swarm.providers.events import EventType, TerminalEvent

        events = [TerminalEvent(EventType.ACCEPT_EDITS, metadata={"has_bash": True})]
        w = _make_worker(state=WorkerState.RESTING)
        content = ">> accept edits on · 1 file, 2 bashes\n> Yes\n  No"
        d = decide(w, content, escalated=escalated, events=events)
        assert d.decision == Decision.ESCALATE
        assert "bash" in d.reason.lower()

    def test_accept_edits_event_without_bash_continues(self, escalated):
        """ACCEPT_EDITS event with has_bash=False should auto-accept."""
        from swarm.providers.events import EventType, TerminalEvent

        events = [TerminalEvent(EventType.ACCEPT_EDITS, metadata={"has_bash": False})]
        w = _make_worker(state=WorkerState.RESTING)
        content = ">> accept edits on · 3 files\n> Yes\n  No"
        d = decide(w, content, escalated=escalated, events=events)
        assert d.decision == Decision.CONTINUE
        assert "files only" in d.reason

    def test_plan_event_escalates(self, escalated):
        """PLAN event should always escalate."""
        from swarm.providers.events import EventType, TerminalEvent

        events = [TerminalEvent(EventType.PLAN)]
        w = _make_worker(state=WorkerState.WAITING)
        content = """\
Plan saved. Proceed with this plan?
> 1. Yes
  2. No
Esc to cancel"""
        d = decide(w, content, escalated=escalated, events=events)
        assert d.decision == Decision.ESCALATE
        assert "plan" in d.reason

    def test_none_events_falls_back_to_regex(self, escalated):
        """When events=None, regex-based detection should still work."""
        w = _make_worker(state=WorkerState.WAITING)
        content = """\
Read file
  Read(/home/user/file.py)
> 1. Yes
  2. No
Esc to cancel"""
        d = decide(w, content, escalated=escalated, events=None)
        assert d.decision == Decision.CONTINUE
        assert "safe operation" in d.reason

    def test_events_on_stung_worker(self, escalated):
        """Events should be attached even for STUNG worker decisions."""
        from swarm.providers.events import EventType, TerminalEvent

        events = [TerminalEvent(EventType.UNKNOWN)]
        w = _make_worker(state=WorkerState.STUNG)
        d = decide(w, "$ ", escalated=escalated, events=events)
        assert d.decision == Decision.REVIVE
        assert d.events is events


class TestSplitlinesCaching:
    """Verify that decide() computes splitlines once and passes to sub-functions."""

    def test_accept_edits_uses_cached_lines(self, escalated):
        """_decide_accept_edits receives pre-split lines (bash in last 5 lines)."""
        w = _make_worker(state=WorkerState.RESTING)
        # ">> accept edits on" triggers has_accept_edits_prompt;
        # "bash" in the last 5 lines triggers escalation
        content = "line1\nline2\nline3\n>> accept edits on · 1 file, 2 bashes\n> Yes\n  No"
        d = decide(w, content, escalated=escalated)
        assert d.decision == Decision.ESCALATE
        assert "bash" in d.reason.lower()

    def test_idle_state_uses_cached_lines(self, escalated):
        """_decide_idle_state uses pre-split lines for tail hint check."""
        w = _make_worker(state=WorkerState.RESTING)
        content = "working...\nsome output\n? for shortcuts\n> "
        d = decide(w, content, escalated=escalated)
        assert d.decision == Decision.NONE
        assert "idle" in d.reason

    def test_choice_uses_cached_lines_for_rule_area(self, escalated):
        """_decide_choice uses pre-split lines for prompt_area and rule_area."""
        from swarm.config import DroneApprovalRule

        # Use a non-safe-builtin pattern so it goes through approval rules
        cfg = DroneConfig(approval_rules=[DroneApprovalRule("npm", "approve")])
        w = _make_worker(state=WorkerState.WAITING)
        # Build content with >15 lines of padding so rule_area (last 15) matters
        padding = "\n".join(f"old output line {i}" for i in range(20))
        content = padding + "\nBash command\n  npm install\n> 1. Yes\n  2. No\nEsc to cancel"
        d = decide(w, content, config=cfg, escalated=escalated)
        assert d.decision == Decision.CONTINUE
        assert d.source == "rule"


class TestDryRun:
    """Unit tests for dry_run_rules()."""

    def test_always_escalate_short_circuits(self):
        from swarm.drones.rules import dry_run_rules

        results = dry_run_rules("DROP TABLE users", approval_rules=[])
        assert len(results) == 1
        r = results[0]
        assert r.matched is True
        assert r.decision == "escalate"
        assert r.source == "always_escalate"

    def test_safe_builtin_approves(self):
        from swarm.drones.rules import dry_run_rules

        results = dry_run_rules("Read(src/main.py)", approval_rules=[])
        assert len(results) == 1
        r = results[0]
        assert r.matched is True
        assert r.decision == "approve"
        assert r.source == "safe_builtin"

    def test_user_rule_first_match_wins(self):
        from swarm.config import DroneApprovalRule
        from swarm.drones.rules import dry_run_rules

        rules = [
            DroneApprovalRule("npm", "approve"),
            DroneApprovalRule("npm install", "escalate"),
        ]
        results = dry_run_rules("npm install express", approval_rules=rules)
        assert len(results) == 1
        r = results[0]
        assert r.matched is True
        assert r.decision == "approve"
        assert r.rule_index == 0
        assert r.rule_pattern == "npm"
        assert r.source == "rule"

    def test_no_match_default_escalate(self):
        from swarm.config import DroneApprovalRule
        from swarm.drones.rules import dry_run_rules

        rules = [DroneApprovalRule("npm", "approve")]
        results = dry_run_rules("something completely different", approval_rules=rules)
        assert len(results) == 1
        r = results[0]
        assert r.matched is False
        assert r.decision == "escalate"
        assert r.source == "default_escalate"


class TestAlwaysEscalatePatterns:
    """Parametrized tests for ALWAYS_ESCALATE safety net patterns."""

    @pytest.mark.parametrize(
        "text",
        [
            "TRUNCATE TABLE users",
            "TRUNCATE users",
            "truncate table logs",
            "rm -r /tmp/data",
            "rm -rf /tmp/data",
            "rm -fr /tmp/data",
            "rm -Irf /tmp/data",
            "rm -vr /tmp/data",
            "`DROP TABLE users`",
            "` DROP TABLE users`",
            "`TRUNCATE TABLE logs`",
            "` TRUNCATE logs`",
            # existing patterns still work
            "DROP TABLE users",
            "DELETE FROM users ;",
            "git push origin --force",
            "git reset --hard",
            "--no-verify",
            # #1526: SQL writes that are not deletions. Both of the first two
            # AUTO-APPROVED against the live rule list before this was added —
            # a worker could grant itself production ADMIN unsupervised.
            'psql -c "UPDATE \\"user\\" SET hub_role=\'ADMIN\';"',
            "UPDATE users SET password='x' WHERE id=1",
            "update members set email='a@b.c' where id=2",
            "INSERT INTO users (email) VALUES ('x@y.z')",
            "insert into audit_log (actor) values ('sys')",
            "INSERT INTO audit SELECT * FROM staging",
        ],
    )
    def test_always_escalates(self, text: str):
        from swarm.drones.rules import dry_run_rules

        results = dry_run_rules(text, approval_rules=[])
        assert len(results) == 1
        r = results[0]
        assert r.decision == "escalate", f"Expected escalate for: {text!r}"
        assert r.source == "always_escalate", f"Expected always_escalate for: {text!r}"

    @pytest.mark.parametrize(
        "text",
        [
            "git push origin feature-branch",
            "git push origin main",  # user-configurable, no longer hardcoded
            "git push upstream master",  # same — opted in via approval_rules
            "npm install express",
            "ls -la",
            "rm file.txt",  # no -r flag
            "truncated the results",  # not SQL TRUNCATE
            # #1526 FALSE-POSITIVE CONTROLS. The UPDATE/INSERT patterns above are
            # shaped to SQL rather than to the words precisely so these stay
            # approvable. A guard that fires on `npm update` gets switched off
            # within a day and then protects nothing.
            "npm update",
            "apt-get update && apt-get install -y postgresql",
            "brew update",
            "update the documentation before shipping",
            'git commit -m "insert into the changelog"',
            "cargo update -p serde",
        ],
    )
    def test_not_always_escalated(self, text: str):
        from swarm.drones.rules import dry_run_rules

        results = dry_run_rules(text, approval_rules=[])
        assert len(results) == 1
        r = results[0]
        assert r.source != "always_escalate", f"Should NOT always_escalate for: {text!r}"
