"""Known configuration keys and tuning parsing helpers."""

from __future__ import annotations

import logging
from typing import Any

from swarm.config.models import ProviderTuning

_log = logging.getLogger("swarm.config")

_KNOWN_TOP_KEYS = {
    "session_name",
    "projects_dir",
    "provider",
    "workers",
    "groups",
    # #1677: operator-defined PTY key sequences shown in the shortcut bar.
    "shortcuts",
    "default_group",
    "watch_interval",
    "drones",
    "queen",
    "notifications",
    "coordination",
    "jira",
    "test",
    "workflows",
    "tool_buttons",
    "action_buttons",
    "task_buttons",
    "llms",
    "provider_overrides",
    "log_level",
    "log_file",
    "port",
    "daemon_url",
    "api_password",
    "integrations",
    "trust_proxy",
    "tunnel_domain",
    "domain",
    "terminal",
    "resources",
    "sandbox",
    # #1576: `serialize_config` writes this section, so omitting it here made every load
    # warn "unrecognized key 'playbooks' … (typo?)" about our OWN output — the same defect
    # already recorded for the jira v2 keys below. The section was never dropped (the
    # loader parses it independently of this list); the warning was the whole bug, and it
    # sent readers hunting for a typo that did not exist.
    "playbooks",
}

_KNOWN_DRONE_KEYS = {
    "enabled",
    "escalation_threshold",
    "poll_interval",
    "poll_interval_buzzing",
    "poll_interval_waiting",
    "poll_interval_resting",
    "auto_approve_yn",
    "max_revive_attempts",
    "max_poll_failures",
    "max_idle_interval",
    "auto_stop_on_complete",
    "auto_approve_assignments",
    "idle_assign_threshold",
    "auto_complete_min_idle",
    "sleeping_poll_interval",
    "sleeping_threshold",
    "inv2_absent_threshold_seconds",
    "stung_reap_timeout",
    "state_thresholds",
    "approval_rules",
    "allowed_read_paths",
    "context_warning_threshold",
    "context_critical_threshold",
    "speculation_enabled",
    "idle_nudge_interval_seconds",
    "idle_nudge_debounce_seconds",
    "idle_nudge_activity_window_seconds",
    "reconcile_interval_seconds",
    "assign_affinity_floor",
    "assign_operator_engagement_minutes",
    "idle_nudge_max_repeats",
    "nudge_idle_for_informational",
    "message_fanout_max_recipients",
    "message_fanout_window_seconds",
    "prompt_collision_window_seconds",
    "suppress_duplicate_handoff",
    "duplicate_title_similarity",
    "native_goal_enabled",
    "native_goal_max_turns",
    "native_loop_coexistence_enabled",
    "native_loop_grace_seconds",
    "task_token_ceiling",
    "standing_loop_daily_token_cap",
    "standing_loop_topics",
    "user_request_plan_mode",
    "dreamer_interval_seconds",
    "dreamer_lookback_hours",
    "dreamer_min_pattern_count",
    "verifier_criteria_synthesis",
    "verifier_enabled",
    "verifier_enforce",
    "verify_reopen_cap",
    "dispatch_enrichment",
    "learning_preload",
}

_KNOWN_QUEEN_KEYS = {
    "cooldown",
    "enabled",
    "system_prompt",
    "min_confidence",
    "max_session_calls",
    "max_session_age",
    "auto_assign_tasks",
    "queen_thread_retention_days",
    "oversight",
}

_KNOWN_OVERSIGHT_KEYS = {
    "enabled",
    "buzzing_threshold_minutes",
    "drift_check_interval_minutes",
    "max_calls_per_hour",
    "operator_engagement_minutes",
    "auto_park_enabled",
    "auto_park_no_progress_checks",
    "auto_park_reject_backoff_seconds",
}

_KNOWN_NOTIFY_KEYS = {
    "terminal_bell",
    "desktop",
    "desktop_events",
    "terminal_events",
    "debounce_seconds",
    "templates",
    "webhook",
    "email",
}

_KNOWN_COORDINATION_KEYS = {"mode", "auto_pull", "file_ownership", "message_retention_days"}

_KNOWN_JIRA_KEYS = {
    "read_only",
    "sprint_priority_boost",
    "enabled",
    "project",
    "sync_interval_minutes",
    "status_map",
    # v2 (docs/specs/jira-integration-v2.md). Missing entries here do not break
    # anything, but every load logs "unrecognized key ... (typo?)" for a key the
    # system itself writes — a warning that trains the operator to ignore warnings.
    "projects",
    "issue_types",
    "project_status_maps",
    "confirmed_projects",
    "client_id",
    "client_secret",
    "cloud_id",
}
# Legacy keys that were removed -- warn if present
# Removed keys — present in older configs, warned about rather than silently dropped.
# import_filter/import_label stopped routing when imports became assignee-based, and
# lookback_days was read by no query at all; leaving them in the model would have
# kept three settings that look live and do nothing.
# Superseded by OAuth: the operator should move the credential, not delete the concept.
_AUTH_STALE_JIRA_KEYS = {
    "url",
    "email",
    "token",
    "auth_mode",
}

# REMOVED outright (2026.8.8.7). import_filter and import_label routed imports by label,
# which does not survive Jira being enabled for every dev — every swarm imported the same
# tickets. lookback_days was read by no query at all. They stay listed so an existing
# swarm.yaml is told the setting is gone rather than being told it looks like a typo.
_REMOVED_JIRA_KEYS = {
    "import_filter",
    "import_label",
    "lookback_days",
    # The GLOBAL status map. Removed 2026.8.8.9: it defaulted to a full hardcoded map,
    # so it silently supplied `done -> "Done"` to every project that had never been
    # mapped — the map 11 IS tickets refused. Transition targets are per-project and
    # discovered.
    "status_map",
}

_STALE_JIRA_KEYS = _AUTH_STALE_JIRA_KEYS | _REMOVED_JIRA_KEYS

_KNOWN_TEST_KEYS = {
    "enabled",
    "port",
    "auto_resolve_delay",
    "report_dir",
    "auto_complete_min_idle",
}

_KNOWN_TERMINAL_KEYS = {
    "replay_scrollback",
    "replay_max_bytes",
    # Deprecated: retained for backward-compatible parsing only.
    "skip_replay_render_on_reconnect",
}


_KNOWN_RESOURCES_KEYS = {
    "enabled",
    "poll_interval",
    "elevated_swap_pct",
    "elevated_mem_pct",
    "high_swap_pct",
    "high_mem_pct",
    "critical_swap_pct",
    "critical_mem_pct",
    "suspend_on_high",
    "dstate_scan",
    "dstate_threshold_sec",
}


_KNOWN_SANDBOX_KEYS = {
    "enabled",
    "min_claude_version",
    "settings_overrides",
}


def _warn_unknown_keys(section: str, data: dict[str, Any], known: set[str]) -> None:
    """Log a warning for any unrecognized keys in a config section."""
    if not isinstance(data, dict):
        return
    unknown = set(data.keys()) - known
    for key in sorted(unknown):
        _log.warning("unrecognized key '%s' in %s section — ignored (typo?)", key, section)


_TUNING_FIELDS = {
    "idle_pattern",
    "busy_pattern",
    "choice_pattern",
    "user_question_pattern",
    "safe_patterns",
    "approval_key",
    "rejection_key",
    "env_strip_prefixes",
    "env_vars",
    "tail_lines",
}


def _parse_tuning(data: dict[str, Any]) -> ProviderTuning:
    """Parse a ProviderTuning from a dict (subset of keys)."""
    esp = data.get("env_strip_prefixes", [])
    if isinstance(esp, str):
        esp = [s.strip() for s in esp.split(",") if s.strip()]
    ev = data.get("env_vars", {})
    if not isinstance(ev, dict):
        ev = {}
    tl = data.get("tail_lines", 0)
    try:
        tl = int(tl)
    except (ValueError, TypeError):
        tl = 0
    return ProviderTuning(
        idle_pattern=str(data.get("idle_pattern", "")),
        busy_pattern=str(data.get("busy_pattern", "")),
        choice_pattern=str(data.get("choice_pattern", "")),
        user_question_pattern=str(data.get("user_question_pattern", "")),
        safe_patterns=str(data.get("safe_patterns", "")),
        approval_key=str(data.get("approval_key", "")),
        rejection_key=str(data.get("rejection_key", "")),
        env_strip_prefixes=list(esp),
        env_vars={str(k): str(v) for k, v in ev.items()},
        tail_lines=tl,
    )
