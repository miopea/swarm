"""Serialization and saving of HiveConfig to YAML."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import yaml

from swarm.config.models import (
    ConfigError,
    HiveConfig,
    PlaybookConfig,
    ProviderTuning,
    QueenConfig,
    ResourceConfig,
    SandboxConfig,
    StateThresholds,
    TestConfig,
    WorkerConfig,
)


def _serialize_queen(q: QueenConfig) -> dict[str, Any]:
    """Serialize QueenConfig, omitting empty system_prompt."""
    d: dict[str, Any] = {
        "cooldown": q.cooldown,
        "enabled": q.enabled,
        "min_confidence": q.min_confidence,
        "max_session_calls": q.max_session_calls,
        "max_session_age": q.max_session_age,
        "auto_assign_tasks": q.auto_assign_tasks,
        "queen_thread_retention_days": q.queen_thread_retention_days,
    }
    if q.system_prompt:
        d["system_prompt"] = q.system_prompt
    d["oversight"] = {
        "enabled": q.oversight.enabled,
        "buzzing_threshold_minutes": q.oversight.buzzing_threshold_minutes,
        "drift_check_interval_minutes": q.oversight.drift_check_interval_minutes,
        "max_calls_per_hour": q.oversight.max_calls_per_hour,
        "operator_engagement_minutes": q.oversight.operator_engagement_minutes,
        "auto_park_enabled": q.oversight.auto_park_enabled,
        "auto_park_no_progress_checks": q.oversight.auto_park_no_progress_checks,
        "auto_park_reject_backoff_seconds": q.oversight.auto_park_reject_backoff_seconds,
    }
    return d


def _serialize_worker(w: WorkerConfig) -> dict[str, Any]:
    """Serialize a WorkerConfig, omitting empty description and provider."""
    d: dict[str, Any] = {"name": w.name, "path": w.path}
    if w.description:
        d["description"] = w.description
    if w.provider:
        d["provider"] = w.provider
    if w.isolation:
        d["isolation"] = w.isolation
    if w.identity:
        d["identity"] = w.identity
    if w.approval_rules:
        d["approval_rules"] = [{"pattern": r.pattern, "action": r.action} for r in w.approval_rules]
    if w.allowed_tools:
        d["allowed_tools"] = list(w.allowed_tools)
    return d


def _serialize_test(t: TestConfig) -> dict[str, Any]:
    """Serialize TestConfig. Always returns a dict (templates access unconditionally)."""
    return {
        "enabled": t.enabled,
        "port": t.port,
        "auto_resolve_delay": t.auto_resolve_delay,
        "report_dir": t.report_dir,
        "auto_complete_min_idle": t.auto_complete_min_idle,
    }


def _serialize_playbooks(p: PlaybookConfig) -> dict[str, Any]:
    """Serialize PlaybookConfig. P4b: full primitive set, no nested types.

    Every field is included unconditionally because the DB layer reads
    this whole dict back through _parse_json_dataclass and silent-drop
    bugs in this chain are exactly what the audit warns against.
    """
    return {
        "enabled": p.enabled,
        "eligible_task_types": list(p.eligible_task_types),
        "min_resolution_chars": p.min_resolution_chars,
        "max_synth_per_hour": p.max_synth_per_hour,
        "auto_promote_uses": p.auto_promote_uses,
        "auto_promote_winrate": p.auto_promote_winrate,
        "prune_min_uses": p.prune_min_uses,
        "prune_max_winrate": p.prune_max_winrate,
        "consolidation_interval_seconds": p.consolidation_interval_seconds,
        "dedupe_similarity_threshold": p.dedupe_similarity_threshold,
        "install_as_native_skills": p.install_as_native_skills,
    }


def _serialize_tuning(tuning: ProviderTuning) -> dict[str, Any]:
    """Serialize a ProviderTuning to a dict, omitting empty/default fields."""
    d: dict[str, Any] = {}
    for key in (
        "idle_pattern",
        "busy_pattern",
        "choice_pattern",
        "user_question_pattern",
        "safe_patterns",
        "approval_key",
        "rejection_key",
    ):
        val = getattr(tuning, key, "")
        if val:
            d[key] = val
    if tuning.env_strip_prefixes:
        d["env_strip_prefixes"] = list(tuning.env_strip_prefixes)
    if tuning.env_vars:
        d["env_vars"] = dict(tuning.env_vars)
    if tuning.tail_lines:
        d["tail_lines"] = tuning.tail_lines
    return d


def _serialize_notifications(config: HiveConfig) -> dict[str, Any]:
    """Serialize NotifyConfig, including webhook if configured."""
    notify_dict: dict[str, Any] = {
        "terminal_bell": config.notifications.terminal_bell,
        "desktop": config.notifications.desktop,
        "debounce_seconds": config.notifications.debounce_seconds,
    }
    if config.notifications.desktop_events:
        notify_dict["desktop_events"] = list(config.notifications.desktop_events)
    if config.notifications.terminal_events:
        notify_dict["terminal_events"] = list(config.notifications.terminal_events)
    if config.notifications.templates:
        notify_dict["templates"] = dict(config.notifications.templates)
    wh_cfg = config.notifications.webhook
    notify_dict["webhook"] = {
        "url": wh_cfg.url,
        "events": list(wh_cfg.events) if wh_cfg.events else [],
    }
    em = config.notifications.email
    notify_dict["email"] = {
        "enabled": em.enabled,
        "smtp_host": em.smtp_host,
        "smtp_port": em.smtp_port,
        "smtp_user": em.smtp_user,
        "smtp_password": em.smtp_password,
        "use_tls": em.use_tls,
        "from_address": em.from_address,
        "to_addresses": list(em.to_addresses),
        "events": list(em.events),
    }
    return notify_dict


def _serialize_llms_optional(config: HiveConfig, data: dict[str, Any]) -> None:
    """Serialize custom LLMs and provider overrides into *data*."""
    if config.custom_llms:
        data["llms"] = [
            {
                "name": llm.name,
                "command": llm.command,
                **({"display_name": llm.display_name} if llm.display_name else {}),
                **(_serialize_tuning(llm.tuning) if llm.tuning.has_tuning() else {}),
            }
            for llm in config.custom_llms
        ]
    if config.provider_overrides:
        overrides_dict: dict[str, Any] = {}
        for pname, tuning in config.provider_overrides.items():
            td = _serialize_tuning(tuning)
            if td:
                overrides_dict[pname] = td
        if overrides_dict:
            data["provider_overrides"] = overrides_dict


def _serialize_terminal_optional(config: HiveConfig, data: dict[str, Any]) -> None:
    if not config.terminal.replay_scrollback:
        data["terminal"] = {
            "replay_scrollback": config.terminal.replay_scrollback,
        }


def _serialize_integrations_optional(config: HiveConfig, data: dict[str, Any]) -> None:
    if config.graph_client_id:
        graph: dict[str, str] = {
            "client_id": config.graph_client_id,
            "tenant_id": config.graph_tenant_id,
        }
        if config.graph_client_secret:
            graph["client_secret"] = config.graph_client_secret
        data["integrations"] = {"graph": graph}


def _serialize_optional(config: HiveConfig, data: dict[str, Any]) -> None:
    """Serialize optional config fields into *data* (mutating). Keeps serialize_config lean."""
    for key, val in (
        ("log_file", config.log_file),
        ("daemon_url", config.daemon_url),
        ("api_password", config.api_password),
    ):
        if val is not None:
            data[key] = val
    if config.workflows:
        data["workflows"] = dict(config.workflows)
    if config.tool_buttons:
        data["tool_buttons"] = [
            {"label": b.label, "command": b.command} for b in config.tool_buttons
        ]
    if config.action_buttons:
        data["action_buttons"] = [
            {
                "label": b.label,
                "action": b.action,
                "command": b.command,
                "style": b.style,
                "show_mobile": b.show_mobile,
                "show_desktop": b.show_desktop,
            }
            for b in config.action_buttons
        ]
    if config.task_buttons:
        data["task_buttons"] = [
            {
                "label": b.label,
                "action": b.action,
                "show_mobile": b.show_mobile,
                "show_desktop": b.show_desktop,
            }
            for b in config.task_buttons
        ]
    _serialize_llms_optional(config, data)
    _serialize_terminal_optional(config, data)
    data["test"] = _serialize_test(config.test)
    # P4b: playbook tuning. Always serialized — config_store reads from
    # the _JSON_KEYS set and silently drops anything that isn't in the
    # outgoing payload, so we surface it unconditionally even when the
    # operator hasn't touched the defaults.
    data["playbooks"] = _serialize_playbooks(config.playbooks)
    if config.trust_proxy:
        data["trust_proxy"] = config.trust_proxy
    if config.tunnel_domain:
        data["tunnel_domain"] = config.tunnel_domain
    _serialize_integrations_optional(config, data)
    _serialize_resources_optional(config, data)
    _serialize_sandbox_optional(config, data)


def _serialize_resources_optional(config: HiveConfig, data: dict[str, Any]) -> None:
    """Serialize ResourceConfig when it diverges from defaults (lean YAML)."""
    r = config.resources
    if r == ResourceConfig():
        return
    data["resources"] = {
        "enabled": r.enabled,
        "poll_interval": r.poll_interval,
        "elevated_swap_pct": r.elevated_swap_pct,
        "elevated_mem_pct": r.elevated_mem_pct,
        "high_swap_pct": r.high_swap_pct,
        "high_mem_pct": r.high_mem_pct,
        "critical_swap_pct": r.critical_swap_pct,
        "critical_mem_pct": r.critical_mem_pct,
        "suspend_on_high": r.suspend_on_high,
        "dstate_scan": r.dstate_scan,
        "dstate_threshold_sec": r.dstate_threshold_sec,
    }


def _serialize_sandbox_optional(config: HiveConfig, data: dict[str, Any]) -> None:
    """Serialize SandboxConfig when it diverges from defaults (lean YAML)."""
    s = config.sandbox
    if s == SandboxConfig():
        return
    data["sandbox"] = {
        "enabled": s.enabled,
        "min_claude_version": s.min_claude_version,
        "settings_overrides": dict(s.settings_overrides),
    }


def _serialize_drones(config: HiveConfig) -> dict[str, Any]:
    """Serialize DroneConfig to a dict."""
    d = config.drones
    drones_dict: dict[str, Any] = {
        "enabled": d.enabled,
        "escalation_threshold": d.escalation_threshold,
        "poll_interval": d.poll_interval,
        "poll_interval_buzzing": d.poll_interval_buzzing,
        "poll_interval_waiting": d.poll_interval_waiting,
        "poll_interval_resting": d.poll_interval_resting,
        "auto_approve_yn": d.auto_approve_yn,
        "max_revive_attempts": d.max_revive_attempts,
        "max_poll_failures": d.max_poll_failures,
        "max_idle_interval": d.max_idle_interval,
        "auto_stop_on_complete": d.auto_stop_on_complete,
        "auto_approve_assignments": d.auto_approve_assignments,
        "idle_assign_threshold": d.idle_assign_threshold,
        "auto_complete_min_idle": d.auto_complete_min_idle,
        "sleeping_poll_interval": d.sleeping_poll_interval,
        "sleeping_threshold": d.sleeping_threshold,
        "stung_reap_timeout": d.stung_reap_timeout,
        "idle_nudge_interval_seconds": d.idle_nudge_interval_seconds,
        "idle_nudge_debounce_seconds": d.idle_nudge_debounce_seconds,
        "reconcile_interval_seconds": d.reconcile_interval_seconds,
        "assign_affinity_floor": d.assign_affinity_floor,
        "assign_operator_engagement_minutes": d.assign_operator_engagement_minutes,
        "context_warning_threshold": d.context_warning_threshold,
        "context_critical_threshold": d.context_critical_threshold,
        "speculation_enabled": d.speculation_enabled,
        "idle_nudge_max_repeats": d.idle_nudge_max_repeats,
        "native_goal_enabled": d.native_goal_enabled,
        "native_goal_max_turns": d.native_goal_max_turns,
        "user_request_plan_mode": d.user_request_plan_mode,
        "dreamer_interval_seconds": d.dreamer_interval_seconds,
        "dreamer_lookback_hours": d.dreamer_lookback_hours,
        "dreamer_min_pattern_count": d.dreamer_min_pattern_count,
        "approval_rules": [{"pattern": r.pattern, "action": r.action} for r in d.approval_rules],
    }
    if d.allowed_read_paths:
        drones_dict["allowed_read_paths"] = list(d.allowed_read_paths)
    st = d.state_thresholds
    default_st = StateThresholds()
    if st != default_st:
        drones_dict["state_thresholds"] = {
            "buzzing_confirm_count": st.buzzing_confirm_count,
            "stung_confirm_count": st.stung_confirm_count,
            "revive_grace": st.revive_grace,
        }
    return drones_dict


def _serialize_jira_optional(config: HiveConfig, data: dict[str, Any]) -> None:
    """Serialize JiraConfig into *data* if enabled or configured."""
    j = config.jira
    if not (j.enabled or j.client_id):
        return
    jira_out: dict[str, object] = {
        "enabled": j.enabled,
        "project": j.project,
        "sync_interval_minutes": j.sync_interval_minutes,
        "import_filter": j.import_filter,
        "import_label": j.import_label,
        "lookback_days": j.lookback_days,
        "status_map": dict(j.status_map),
    }
    if j.client_id:
        jira_out["client_id"] = j.client_id
    if j.client_secret:
        jira_out["client_secret"] = j.client_secret
    if j.cloud_id:
        jira_out["cloud_id"] = j.cloud_id
    data["jira"] = jira_out


def serialize_config(config: HiveConfig) -> dict[str, Any]:
    """Full round-trip serialization of HiveConfig to a dict. Omits None optional fields."""
    data: dict[str, Any] = {
        "session_name": config.session_name,
        "projects_dir": config.projects_dir,
        "provider": config.provider,
        "port": config.port,
        "watch_interval": config.watch_interval,
        "log_level": config.log_level,
        "workers": [_serialize_worker(w) for w in config.workers],
        "groups": [{"name": g.name, "workers": g.workers} for g in config.groups],
    }
    if config.default_group:
        data["default_group"] = config.default_group
    data["drones"] = _serialize_drones(config)
    data["queen"] = _serialize_queen(config.queen)
    data["notifications"] = _serialize_notifications(config)
    data["coordination"] = {
        "mode": config.coordination.mode,
        "auto_pull": config.coordination.auto_pull,
        "file_ownership": config.coordination.file_ownership,
    }
    _serialize_jira_optional(config, data)
    _serialize_optional(config, data)
    if config.domain:
        data["domain"] = config.domain
    return data


def save_config(config: HiveConfig, path: str | None = None) -> None:
    """Write full YAML config. Requires explicit path or config.source_path."""
    import shutil

    _save_log = logging.getLogger("swarm.config.save")
    resolved = path or config.source_path
    if not resolved:
        raise ConfigError("save_config called with no path and no source_path — refusing to write")
    target = Path(resolved)
    data = serialize_config(config)

    # Safety: refuse to overwrite a config that had workers with an empty one
    if target.exists() and not data.get("workers"):
        try:
            existing = yaml.safe_load(target.read_text()) or {}
            if existing.get("workers"):
                _save_log.error(
                    "BLOCKED: save_config would wipe %d workers — refusing to write",
                    len(existing["workers"]),
                )
                return
        except OSError:
            _save_log.debug("could not read existing config for safety check")
            pass  # Can't read existing -- proceed cautiously

    # Backup before writing (keep one .bak copy)
    if target.exists():
        bak = target.with_suffix(".yaml.bak")
        try:
            shutil.copy2(str(target), str(bak))
        except Exception:
            _save_log.debug("could not create backup at %s", bak)

    import tempfile

    # Atomic write: write to temp file then rename (prevents partial writes on crash)
    content = yaml.dump(data, default_flow_style=False, sort_keys=False)
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), suffix=".tmp")
    closed = False
    try:
        os.write(fd, content.encode())
        os.fchmod(fd, 0o600)
        os.close(fd)
        closed = True
        os.replace(tmp, str(target))
    except BaseException:
        if not closed:
            os.close(fd)
        Path(tmp).unlink(missing_ok=True)
        raise
