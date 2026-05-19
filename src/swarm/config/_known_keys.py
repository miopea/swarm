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
    "stung_reap_timeout",
    "state_thresholds",
    "approval_rules",
    "allowed_read_paths",
    "context_warning_threshold",
    "context_critical_threshold",
    "speculation_enabled",
    "idle_nudge_interval_seconds",
    "idle_nudge_debounce_seconds",
    "assign_affinity_floor",
    "assign_operator_engagement_minutes",
}

_KNOWN_QUEEN_KEYS = {
    "cooldown",
    "enabled",
    "system_prompt",
    "min_confidence",
    "max_session_calls",
    "max_session_age",
    "auto_assign_tasks",
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

_KNOWN_COORDINATION_KEYS = {"mode", "auto_pull", "file_ownership"}

_KNOWN_JIRA_KEYS = {
    "enabled",
    "project",
    "sync_interval_minutes",
    "import_filter",
    "import_label",
    "lookback_days",
    "status_map",
    "client_id",
    "client_secret",
    "cloud_id",
}
# Legacy keys that were removed -- warn if present
_STALE_JIRA_KEYS = {"url", "email", "token", "auth_mode"}

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
