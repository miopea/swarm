"""Validation functions for HiveConfig."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from swarm.config.models import HiveConfig

from swarm.config.models import _validate_tuning_patterns


def validate_config(config: HiveConfig) -> list[str]:
    """Validate config, returning a list of error messages (empty = valid)."""
    errors: list[str] = []
    errors.extend(_validate_workers(config))
    errors.extend(_validate_groups(config))
    errors.extend(_validate_numeric_ranges(config))
    errors.extend(_validate_provider_overrides(config))
    errors.extend(_validate_notifications(config))
    return errors


def _validate_notifications(config: HiveConfig) -> list[str]:
    """Check notification event type names are valid."""
    from swarm.notify.bus import EventType

    known = {e.value for e in EventType}
    errors: list[str] = []
    for label, events in [
        ("desktop_events", config.notifications.desktop_events),
        ("terminal_events", config.notifications.terminal_events),
        ("webhook.events", config.notifications.webhook.events),
        ("email.events", config.notifications.email.events),
    ]:
        for ev in events:
            if ev and ev not in known:
                errors.append(f"notifications.{label}: unknown event type '{ev}'")
    for key in config.notifications.templates:
        if key not in known:
            errors.append(f"notifications.templates: unknown event type '{key}'")
    return errors


def _validate_workers(config: HiveConfig) -> list[str]:
    """Check worker definitions: existence, duplicates, paths, providers."""
    errors: list[str] = []
    if not config.workers:
        errors.append("No workers defined — add at least one worker to swarm.yaml")
    names = [w.name.lower() for w in config.workers]
    seen: set[str] = set()
    for n in names:
        if n in seen:
            errors.append(f"Duplicate worker name: '{n}'")
        seen.add(n)
    for w in config.workers:
        p = w.resolved_path
        if not p.exists():
            errors.append(f"Worker '{w.name}' path does not exist: {p}")
    # Validate provider names
    from swarm.providers import get_valid_providers

    valid = get_valid_providers()
    if config.provider not in valid:
        errors.append(f"Global provider '{config.provider}' is unknown")
    for w in config.workers:
        if w.provider and w.provider not in valid:
            errors.append(f"Worker '{w.name}' has unknown provider '{w.provider}'")
    errors.extend(_validate_custom_llms(config, valid))
    return errors


def _validate_custom_llms(config: HiveConfig, builtin_names: frozenset[str]) -> list[str]:
    """Validate custom LLM definitions: no empty names, duplicates, or built-in collisions."""
    errors: list[str] = []
    seen: set[str] = set()
    for i, llm in enumerate(config.custom_llms):
        if not llm.name:
            errors.append(f"llms[{i}]: name is required")
        elif llm.name in builtin_names:
            errors.append(f"llms[{i}]: name '{llm.name}' collides with built-in provider")
        elif llm.name in seen:
            errors.append(f"llms[{i}]: duplicate name '{llm.name}'")
        else:
            seen.add(llm.name)
        if not llm.command:
            errors.append(f"llms[{i}]: command is required")
    return errors


def _validate_groups(config: HiveConfig) -> list[str]:
    """Check group references and duplicate group names."""
    errors: list[str] = []
    valid_names = {w.name.lower() for w in config.workers}
    for g in config.groups:
        for member in g.workers:
            if member.lower() not in valid_names:
                errors.append(f"Group '{g.name}' references unknown worker: '{member}'")
    gnames = [g.name.lower() for g in config.groups]
    seen_g: set[str] = set()
    for gn in gnames:
        if gn in seen_g:
            errors.append(f"Duplicate group name: '{gn}'")
        seen_g.add(gn)
    if config.default_group:
        group_names_set = {g.name.lower() for g in config.groups}
        if config.default_group.lower() not in group_names_set:
            errors.append(
                f"default_group '{config.default_group}' does not match any defined group"
            )
    return errors


def _validate_numeric_ranges(config: HiveConfig) -> list[str]:
    """Check numeric field ranges, paths, and approval rule patterns."""
    errors: list[str] = []
    if config.log_file:
        log_parent = Path(config.log_file).expanduser().parent
        if not log_parent.exists():
            errors.append(f"Log file parent directory does not exist: {log_parent}")
    if config.watch_interval <= 0:
        errors.append("watch_interval must be > 0")
    if not (1 <= config.port <= 65535):
        errors.append(f"port must be between 1 and 65535, got {config.port}")
    if not (1 <= config.test.port <= 65535):
        errors.append(f"test.port must be between 1 and 65535, got {config.test.port}")
    errors.extend(_validate_drone_ranges(config))
    errors.extend(_validate_queen_ranges(config))
    errors.extend(_validate_resource_ranges(config))
    errors.extend(_validate_approval_rules(config))
    errors.extend(_validate_coordination(config))
    errors.extend(_validate_jira(config))
    return errors


def _validate_drone_ranges(config: HiveConfig) -> list[str]:
    """Validate drone-specific numeric config fields."""
    errors: list[str] = []
    d = config.drones
    if d.poll_interval <= 0:
        errors.append("drones.poll_interval must be > 0")
    if d.escalation_threshold <= 0:
        errors.append("drones.escalation_threshold must be > 0")
    if d.max_revive_attempts < 0:
        errors.append("drones.max_revive_attempts must be >= 0")
    if d.max_poll_failures < 1:
        errors.append("drones.max_poll_failures must be >= 1")
    if d.sleeping_poll_interval <= 0:
        errors.append("drones.sleeping_poll_interval must be > 0")
    if d.sleeping_threshold <= 0:
        errors.append("drones.sleeping_threshold must be > 0")
    if d.stung_reap_timeout <= 0:
        errors.append("drones.stung_reap_timeout must be > 0")
    if d.idle_assign_threshold < 1:
        errors.append("drones.idle_assign_threshold must be >= 1")
    if not (0.0 <= d.assign_affinity_floor <= 1.0):
        errors.append("drones.assign_affinity_floor must be between 0.0 and 1.0")
    if d.assign_operator_engagement_minutes < 0:
        errors.append("drones.assign_operator_engagement_minutes must be >= 0")
    return errors


def _validate_queen_ranges(config: HiveConfig) -> list[str]:
    """Validate queen-specific numeric config fields."""
    errors: list[str] = []
    q = config.queen
    if q.cooldown < 0:
        errors.append("queen.cooldown must be >= 0")
    if not (0.0 <= q.min_confidence <= 1.0):
        errors.append("queen.min_confidence must be between 0.0 and 1.0")
    if q.max_session_calls < 1:
        errors.append("queen.max_session_calls must be >= 1")
    if q.max_session_age <= 0:
        errors.append("queen.max_session_age must be > 0")
    o = q.oversight
    if o.buzzing_threshold_minutes <= 0:
        errors.append("queen.oversight.buzzing_threshold_minutes must be > 0")
    if o.drift_check_interval_minutes <= 0:
        errors.append("queen.oversight.drift_check_interval_minutes must be > 0")
    if o.max_calls_per_hour < 1:
        errors.append("queen.oversight.max_calls_per_hour must be >= 1")
    if o.operator_engagement_minutes < 0:
        errors.append("queen.oversight.operator_engagement_minutes must be >= 0")
    return errors


def _validate_resource_ranges(config: HiveConfig) -> list[str]:
    """Validate resource monitoring config fields."""
    errors: list[str] = []
    r = config.resources
    if r.poll_interval < 5.0:
        errors.append("resources.poll_interval must be >= 5.0")
    for name in (
        "elevated_swap_pct",
        "elevated_mem_pct",
        "high_swap_pct",
        "high_mem_pct",
        "critical_swap_pct",
        "critical_mem_pct",
    ):
        val = getattr(r, name)
        if not (0.0 <= val <= 100.0):
            errors.append(f"resources.{name} must be between 0 and 100, got {val}")
    # Check ordering: elevated < high < critical
    if r.elevated_swap_pct >= r.high_swap_pct:
        errors.append("resources: elevated_swap_pct must be < high_swap_pct")
    if r.high_swap_pct >= r.critical_swap_pct:
        errors.append("resources: high_swap_pct must be < critical_swap_pct")
    if r.elevated_mem_pct >= r.high_mem_pct:
        errors.append("resources: elevated_mem_pct must be < high_mem_pct")
    if r.high_mem_pct >= r.critical_mem_pct:
        errors.append("resources: high_mem_pct must be < critical_mem_pct")
    if r.dstate_threshold_sec <= 0:
        errors.append("resources.dstate_threshold_sec must be > 0")
    return errors


def _validate_coordination(config: HiveConfig) -> list[str]:
    """Validate coordination config fields."""
    errors: list[str] = []
    c = config.coordination
    if c.mode not in ("single-branch", "worktree"):
        errors.append(f"coordination.mode must be 'single-branch' or 'worktree', got '{c.mode}'")
    if c.file_ownership not in ("off", "warning", "hard-block"):
        errors.append(
            f"coordination.file_ownership must be 'off', 'warning', "
            f"or 'hard-block', got '{c.file_ownership}'"
        )
    if not isinstance(c.message_retention_days, int) or c.message_retention_days < 0:
        errors.append(
            f"coordination.message_retention_days must be a non-negative integer "
            f"(0 = keep forever), got '{c.message_retention_days}'"
        )
    return errors


def _validate_jira(config: HiveConfig) -> list[str]:
    """Validate Jira integration config fields (OAuth 2.0 only)."""
    errors: list[str] = []
    j = config.jira
    if j.enabled:
        if not j.client_id:
            errors.append("jira.client_id is required when jira is enabled")
        if not j.client_secret:
            errors.append("jira.client_secret is required when jira is enabled")
        if not j.project:
            errors.append("jira.project is required when jira is enabled")
    if j.sync_interval_minutes <= 0:
        errors.append("jira.sync_interval_minutes must be > 0")
    return errors


def _validate_approval_rules(config: HiveConfig) -> list[str]:
    """Validate drone approval rule regex patterns and action values."""
    errors: list[str] = []
    for i, rule in enumerate(config.drones.approval_rules):
        # ``DroneApprovalRule.__post_init__`` already compiled the pattern
        # and stashed any ``re.error`` on the dataclass — read it instead
        # of recompiling.
        if rule.compile_error is not None:
            errors.append(
                f"drones.approval_rules[{i}]: invalid regex '{rule.pattern}': {rule.compile_error}"
            )
        if rule.action not in ("approve", "escalate"):
            errors.append(
                f"drones.approval_rules[{i}]: action must be 'approve' or 'escalate', "
                f"got '{rule.action}'"
            )
    return errors


def _validate_provider_overrides(config: HiveConfig) -> list[str]:
    """Validate provider_overrides: keys must be known providers, regex must compile."""
    errors: list[str] = []
    from swarm.providers import get_valid_providers

    valid = get_valid_providers()
    for pname, tuning in config.provider_overrides.items():
        if pname not in valid:
            errors.append(f"provider_overrides: unknown provider '{pname}'")
        errors.extend(_validate_tuning_patterns(f"provider_overrides.{pname}", tuning))
    for i, llm in enumerate(config.custom_llms):
        if llm.tuning.has_tuning():
            errors.extend(_validate_tuning_patterns(f"llms[{i}]", llm.tuning))
    return errors
