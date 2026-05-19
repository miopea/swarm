"""YAML configuration loading and writing."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import yaml

from swarm.config._known_keys import (
    _KNOWN_COORDINATION_KEYS,
    _KNOWN_DRONE_KEYS,
    _KNOWN_JIRA_KEYS,
    _KNOWN_NOTIFY_KEYS,
    _KNOWN_OVERSIGHT_KEYS,
    _KNOWN_QUEEN_KEYS,
    _KNOWN_RESOURCES_KEYS,
    _KNOWN_TERMINAL_KEYS,
    _KNOWN_TEST_KEYS,
    _KNOWN_TOP_KEYS,
    _STALE_JIRA_KEYS,
    _TUNING_FIELDS,
    _parse_tuning,
    _warn_unknown_keys,
)
from swarm.config.models import (
    DEFAULT_ACTION_BUTTONS,
    DEFAULT_TASK_BUTTONS,
    ActionButtonConfig,
    ConfigError,
    CoordinationConfig,
    CustomLLMConfig,
    DroneApprovalRule,
    DroneConfig,
    EmailConfig,
    GroupConfig,
    HiveConfig,
    JiraConfig,
    NotifyConfig,
    OversightConfig,
    PlaybookConfig,
    ProviderTuning,
    QueenConfig,
    ResourceConfig,
    StateThresholds,
    TaskButtonConfig,
    TerminalConfig,
    TestConfig,
    ToolButtonConfig,
    WebhookConfig,
    WorkerConfig,
)

_log = logging.getLogger("swarm.config")


def _load_dotenv(directory: Path) -> None:
    """Load .env file from directory into os.environ (won't overwrite existing vars)."""
    env_file = directory / ".env"
    if not env_file.is_file():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("\"'")
        if key and key not in os.environ:
            os.environ[key] = value


def _maybe_hash_password(config: HiveConfig) -> None:
    """If api_password is plaintext, hash it and rewrite the YAML file."""
    from swarm.auth.password import hash_password, is_hashed

    pw = config.api_password
    if not pw or is_hashed(pw):
        return
    hashed = hash_password(pw)
    config.api_password = hashed
    # Rewrite YAML so the plaintext password is no longer on disk
    if config.source_path:
        try:
            from swarm.config.serialization import save_config

            save_config(config)
            _log.info("Hashed plaintext api_password in %s", config.source_path)
        except Exception:
            _log.warning("Could not rewrite config to hash api_password", exc_info=True)


def load_config(path: str | None = None) -> HiveConfig:
    """Load config from explicit path, swarm.yaml in CWD, or ~/.config/swarm/config.yaml."""
    candidates = []
    if path:
        candidates.append(Path(path))
    else:
        candidates.append(Path.cwd() / "swarm.yaml")
        candidates.append(Path.home() / ".config" / "swarm" / "config.yaml")

    for candidate in candidates:
        if candidate.exists():
            _log.info("Loading config from %s", candidate)
            _load_dotenv(candidate.parent)
            config = _parse_config(candidate)
            _maybe_hash_password(config)
            return config

    _log.info("No config file found, using auto-detected defaults")
    # Return default config with auto-detected workers
    return _auto_detect_config()


def _parse_llms_and_overrides(
    data: dict[str, Any],
) -> tuple[list[CustomLLMConfig], dict[str, ProviderTuning]]:
    """Parse llms and provider_overrides sections from raw YAML data."""
    llms_raw = data.get("llms", [])
    custom_llms = [
        CustomLLMConfig(
            name=entry.get("name", ""),
            command=entry.get("command", []),
            display_name=entry.get("display_name", ""),
            tuning=(
                _parse_tuning(entry)
                if any(entry.get(k) for k in _TUNING_FIELDS)
                else ProviderTuning()
            ),
        )
        for entry in llms_raw
        if isinstance(entry, dict) and entry.get("name")
    ]
    overrides_raw = data.get("provider_overrides", {})
    provider_overrides: dict[str, ProviderTuning] = {}
    if isinstance(overrides_raw, dict):
        for pname, pdata in overrides_raw.items():
            if isinstance(pdata, dict):
                tuning = _parse_tuning(pdata)
                if tuning.has_tuning():
                    provider_overrides[str(pname)] = tuning
    return custom_llms, provider_overrides


def _parse_jira_section(jira_data: dict[str, object]) -> JiraConfig:
    """Parse the ``jira:`` config section into a JiraConfig."""
    _warn_unknown_keys("jira", jira_data, _KNOWN_JIRA_KEYS | _STALE_JIRA_KEYS)
    stale_found = set(jira_data) & _STALE_JIRA_KEYS
    if stale_found:
        _log.warning(
            "jira config contains legacy keys %s — these are ignored; "
            "use OAuth (client_id/client_secret) instead",
            stale_found,
        )
    default_status_map = {
        "backlog": "To Do",
        "unassigned": "To Do",
        "assigned": "To Do",
        "active": "In Progress",
        "done": "Done",
        "failed": "To Do",
    }
    raw_status_map = jira_data.get("status_map")
    if not isinstance(raw_status_map, dict):
        raw_status_map = {}
    jira_status_map = {**default_status_map, **raw_status_map}
    return JiraConfig(
        enabled=jira_data.get("enabled", False),
        project=jira_data.get("project", ""),
        sync_interval_minutes=jira_data.get("sync_interval_minutes", 5.0),
        import_filter=jira_data.get("import_filter", ""),
        import_label=jira_data.get("import_label", ""),
        lookback_days=int(jira_data.get("lookback_days", 30)),
        status_map=jira_status_map,
        client_id=jira_data.get("client_id", ""),
        client_secret=jira_data.get("client_secret", ""),
        cloud_id=jira_data.get("cloud_id", ""),
    )


def _apply_config_layering(data: dict) -> list[dict]:
    """Merge defaults → group settings → worker settings (later wins).

    Supports a top-level ``defaults:`` section and per-group overrides
    (non-structural keys on group entries). Backward compatible — configs
    without ``defaults:`` return workers unchanged.
    """
    defaults = data.get("defaults", {})
    group_settings: dict[str, dict] = {}
    for g in data.get("groups", []):
        if isinstance(g, dict):
            extras = {k: v for k, v in g.items() if k not in ("name", "workers")}
            if extras:
                for wn in g.get("workers", []):
                    group_settings[wn] = extras

    result = []
    for w in data.get("workers", []):
        name = w.get("name", "")
        merged = {**defaults, **group_settings.get(name, {}), **w}
        result.append(merged)
    return result


def _parse_notifications(notify_data: dict[str, Any]) -> NotifyConfig:
    """Parse the ``notifications:`` section of swarm.yaml into a NotifyConfig."""
    _warn_unknown_keys("notifications", notify_data, _KNOWN_NOTIFY_KEYS)
    webhook_data = notify_data.get("webhook") or {}
    webhook = WebhookConfig(
        url=webhook_data.get("url", ""),
        events=list(webhook_data.get("events") or []),
    )
    email_data = notify_data.get("email") or {}
    try:
        smtp_port = int(email_data.get("smtp_port", 587))
    except (ValueError, TypeError):
        smtp_port = 587
    email = EmailConfig(
        enabled=bool(email_data.get("enabled", False)),
        smtp_host=str(email_data.get("smtp_host", "localhost")),
        smtp_port=smtp_port,
        smtp_user=str(email_data.get("smtp_user", "")),
        smtp_password=str(email_data.get("smtp_password", "")),
        use_tls=bool(email_data.get("use_tls", True)),
        from_address=str(email_data.get("from_address", "")),
        to_addresses=list(email_data.get("to_addresses") or []),
        events=list(email_data.get("events") or []),
    )
    templates_raw = notify_data.get("templates") or {}
    templates = (
        {str(k): str(v) for k, v in templates_raw.items()}
        if isinstance(templates_raw, dict)
        else {}
    )
    return NotifyConfig(
        terminal_bell=notify_data.get("terminal_bell", True),
        desktop=notify_data.get("desktop", True),
        desktop_events=list(notify_data.get("desktop_events") or []),
        terminal_events=list(notify_data.get("terminal_events") or []),
        debounce_seconds=notify_data.get("debounce_seconds", 5.0),
        templates=templates,
        webhook=webhook,
        email=email,
    )


def _parse_config(path: Path) -> HiveConfig:
    data = yaml.safe_load(path.read_text()) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"Expected YAML mapping at top level, got {type(data).__name__}")
    _warn_unknown_keys("top-level", data, _KNOWN_TOP_KEYS)

    merged_workers = _apply_config_layering(data)

    try:
        workers = [
            WorkerConfig(
                name=m["name"],
                path=m["path"],
                description=m.get("description", ""),
                provider=m.get("provider", ""),
                isolation=m.get("isolation", ""),
                identity=m.get("identity", ""),
                approval_rules=[
                    DroneApprovalRule(
                        pattern=r.get("pattern", ""),
                        action=r.get("action", "approve"),
                    )
                    for r in m.get("approval_rules", [])
                    if isinstance(r, dict)
                ],
                allowed_tools=m.get("allowed_tools", []),
            )
            for m in merged_workers
        ]
    except (KeyError, TypeError) as exc:
        raise ConfigError(f"Worker entry missing required field 'name' or 'path': {exc}") from exc

    try:
        groups = [GroupConfig(name=g["name"], workers=g["workers"]) for g in data.get("groups", [])]
    except (KeyError, TypeError) as exc:
        raise ConfigError(
            f"Group entry must be a dict with 'name' and 'workers' fields: {exc}"
        ) from exc

    # Parse drones section
    drones_data = data.get("drones") or {}
    _warn_unknown_keys("drones", drones_data, _KNOWN_DRONE_KEYS)
    approval_rules_raw = drones_data.get("approval_rules", [])
    approval_rules = [
        DroneApprovalRule(
            pattern=r.get("pattern", ""),
            action=r.get("action", "approve"),
        )
        for r in approval_rules_raw
        if isinstance(r, dict)
    ]
    # Parse state_thresholds sub-section
    st_data = drones_data.get("state_thresholds") or {}
    state_thresholds = StateThresholds(
        buzzing_confirm_count=int(st_data.get("buzzing_confirm_count", 12)),
        stung_confirm_count=int(st_data.get("stung_confirm_count", 2)),
        revive_grace=float(st_data.get("revive_grace", 15.0)),
    )
    drones = DroneConfig(
        enabled=drones_data.get("enabled", True),
        escalation_threshold=drones_data.get("escalation_threshold", 120.0),
        poll_interval=drones_data.get("poll_interval", 5.0),
        poll_interval_buzzing=drones_data.get("poll_interval_buzzing", 0.0),
        poll_interval_waiting=drones_data.get("poll_interval_waiting", 0.0),
        poll_interval_resting=drones_data.get("poll_interval_resting", 0.0),
        auto_approve_yn=drones_data.get("auto_approve_yn", False),
        max_revive_attempts=drones_data.get("max_revive_attempts", 3),
        max_poll_failures=drones_data.get("max_poll_failures", 5),
        max_idle_interval=drones_data.get("max_idle_interval", 30.0),
        auto_stop_on_complete=drones_data.get("auto_stop_on_complete", True),
        auto_approve_assignments=drones_data.get("auto_approve_assignments", True),
        idle_assign_threshold=drones_data.get("idle_assign_threshold", 3),
        auto_complete_min_idle=drones_data.get("auto_complete_min_idle", 45.0),
        sleeping_poll_interval=drones_data.get("sleeping_poll_interval", 30.0),
        sleeping_threshold=drones_data.get("sleeping_threshold", 900.0),
        stung_reap_timeout=drones_data.get("stung_reap_timeout", 30.0),
        state_thresholds=state_thresholds,
        approval_rules=approval_rules,
        allowed_read_paths=drones_data.get("allowed_read_paths", []),
        context_warning_threshold=drones_data.get("context_warning_threshold", 0.7),
        context_critical_threshold=drones_data.get("context_critical_threshold", 0.9),
        speculation_enabled=drones_data.get("speculation_enabled", False),
        idle_nudge_interval_seconds=drones_data.get("idle_nudge_interval_seconds", 180.0),
        idle_nudge_debounce_seconds=drones_data.get("idle_nudge_debounce_seconds", 900.0),
        assign_affinity_floor=drones_data.get("assign_affinity_floor", 0.5),
        assign_operator_engagement_minutes=drones_data.get(
            "assign_operator_engagement_minutes", 10.0
        ),
    )

    # Parse queen section
    queen_data = data.get("queen") or {}
    _warn_unknown_keys("queen", queen_data, _KNOWN_QUEEN_KEYS)
    oversight_data = queen_data.get("oversight") or {}
    _warn_unknown_keys("queen.oversight", oversight_data, _KNOWN_OVERSIGHT_KEYS)
    oversight = OversightConfig(
        enabled=oversight_data.get("enabled", True),
        buzzing_threshold_minutes=oversight_data.get("buzzing_threshold_minutes", 15.0),
        drift_check_interval_minutes=oversight_data.get("drift_check_interval_minutes", 10.0),
        max_calls_per_hour=oversight_data.get("max_calls_per_hour", 6),
        operator_engagement_minutes=oversight_data.get("operator_engagement_minutes", 10.0),
        auto_park_enabled=oversight_data.get("auto_park_enabled", True),
        auto_park_no_progress_checks=oversight_data.get("auto_park_no_progress_checks", 3),
        auto_park_reject_backoff_seconds=oversight_data.get(
            "auto_park_reject_backoff_seconds", 7200.0
        ),
    )
    queen = QueenConfig(
        cooldown=queen_data.get("cooldown", 30.0),
        enabled=queen_data.get("enabled", True),
        system_prompt=queen_data.get("system_prompt", ""),
        min_confidence=queen_data.get("min_confidence", 0.7),
        max_session_calls=queen_data.get("max_session_calls", 20),
        max_session_age=queen_data.get("max_session_age", 1800.0),
        auto_assign_tasks=queen_data.get("auto_assign_tasks", True),
        oversight=oversight,
    )

    # Parse terminal section
    terminal_data = data.get("terminal") or {}
    _warn_unknown_keys("terminal", terminal_data, _KNOWN_TERMINAL_KEYS)
    if "skip_replay_render_on_reconnect" in terminal_data:
        _log.warning("terminal.skip_replay_render_on_reconnect is deprecated and ignored")
    if "replay_max_bytes" in terminal_data:
        _log.warning(
            "terminal.replay_max_bytes is deprecated and ignored"
            " — render_ansi() output is bounded by screen size"
        )
    terminal = TerminalConfig(
        replay_scrollback=terminal_data.get("replay_scrollback", True),
    )

    # Parse resources section
    resources_data = data.get("resources") or {}
    _warn_unknown_keys("resources", resources_data, _KNOWN_RESOURCES_KEYS)
    resources = ResourceConfig(
        enabled=resources_data.get("enabled", True),
        poll_interval=resources_data.get("poll_interval", 10.0),
        elevated_swap_pct=resources_data.get("elevated_swap_pct", 40.0),
        elevated_mem_pct=resources_data.get("elevated_mem_pct", 80.0),
        high_swap_pct=resources_data.get("high_swap_pct", 70.0),
        high_mem_pct=resources_data.get("high_mem_pct", 90.0),
        critical_swap_pct=resources_data.get("critical_swap_pct", 85.0),
        critical_mem_pct=resources_data.get("critical_mem_pct", 95.0),
        suspend_on_high=resources_data.get("suspend_on_high", True),
        dstate_scan=resources_data.get("dstate_scan", True),
        dstate_threshold_sec=resources_data.get("dstate_threshold_sec", 120.0),
    )

    notifications = _parse_notifications(data.get("notifications") or {})

    # Parse coordination section
    coord_data = data.get("coordination") or {}
    _warn_unknown_keys("coordination", coord_data, _KNOWN_COORDINATION_KEYS)
    coordination = CoordinationConfig(
        mode=coord_data.get("mode", "single-branch"),
        auto_pull=coord_data.get("auto_pull", True),
        file_ownership=coord_data.get("file_ownership", "warning"),
    )

    jira = _parse_jira_section(data.get("jira") or {})

    # Parse integrations section
    integrations = data.get("integrations", {})
    graph_data = integrations.get("graph", {}) if isinstance(integrations, dict) else {}

    # Parse tool_buttons section (legacy)
    tool_buttons_raw = data.get("tool_buttons", [])
    tool_buttons = [
        ToolButtonConfig(label=b.get("label", ""), command=b.get("command", ""))
        for b in tool_buttons_raw
        if isinstance(b, dict) and b.get("label")
    ]

    # Parse action_buttons -- unified reorderable action bar
    action_buttons_raw = data.get("action_buttons", [])
    if action_buttons_raw:
        action_buttons = [
            ActionButtonConfig(
                label=b.get("label", ""),
                action=b.get("action", ""),
                command=b.get("command", ""),
                style=b.get("style", "secondary"),
                show_mobile=b.get("show_mobile", True),
                show_desktop=b.get("show_desktop", True),
            )
            for b in action_buttons_raw
            if isinstance(b, dict) and b.get("label")
        ]
    else:
        # Backward compat: build from defaults + legacy tool_buttons
        action_buttons = list(DEFAULT_ACTION_BUTTONS)
        for tb in tool_buttons:
            action_buttons.append(
                ActionButtonConfig(label=tb.label, command=tb.command, style="secondary")
            )

    # Parse task_buttons -- configurable task-list buttons
    task_buttons_raw = data.get("task_buttons", [])
    if task_buttons_raw:
        task_buttons = [
            TaskButtonConfig(
                label=b.get("label", ""),
                action=b.get("action", ""),
                show_mobile=b.get("show_mobile", True),
                show_desktop=b.get("show_desktop", True),
            )
            for b in task_buttons_raw
            if isinstance(b, dict) and b.get("label") and b.get("action")
        ]
    else:
        task_buttons = list(DEFAULT_TASK_BUTTONS)

    custom_llms, provider_overrides = _parse_llms_and_overrides(data)

    # Parse test section
    test_data = data.get("test") or {}
    _warn_unknown_keys("test", test_data, _KNOWN_TEST_KEYS)
    try:
        test_port = int(test_data.get("port", 9091))
    except (ValueError, TypeError):
        test_port = 9091
    test = TestConfig(
        enabled=test_data.get("enabled", False),
        port=test_port,
        auto_resolve_delay=test_data.get("auto_resolve_delay", 4.0),
        report_dir=test_data.get("report_dir", "~/.swarm/reports"),
        auto_complete_min_idle=test_data.get("auto_complete_min_idle", 10.0),
    )

    # Parse workflows section -- maps task type names to skill commands
    workflows_raw = data.get("workflows", {})
    workflows = (
        {k: str(v) for k, v in workflows_raw.items() if isinstance(k, str) and v}
        if isinstance(workflows_raw, dict)
        else {}
    )

    pb_data = data.get("playbooks") or {}
    playbooks = PlaybookConfig(
        enabled=pb_data.get("enabled", True),
        eligible_task_types=pb_data.get("eligible_task_types", ["feature", "bug", "chore"]),
        min_resolution_chars=pb_data.get("min_resolution_chars", 80),
        max_synth_per_hour=pb_data.get("max_synth_per_hour", 20),
        auto_promote_uses=pb_data.get("auto_promote_uses", 3),
        auto_promote_winrate=pb_data.get("auto_promote_winrate", 0.7),
        prune_min_uses=pb_data.get("prune_min_uses", 5),
        prune_max_winrate=pb_data.get("prune_max_winrate", 0.3),
        consolidation_interval_seconds=pb_data.get("consolidation_interval_seconds", 21600.0),
        dedupe_similarity_threshold=pb_data.get("dedupe_similarity_threshold", 0.75),
        install_as_native_skills=pb_data.get("install_as_native_skills", True),
    )

    return HiveConfig(
        session_name=data.get("session_name", "swarm"),
        projects_dir=data.get("projects_dir", "~/projects"),
        provider=data.get("provider", "claude"),
        workers=workers,
        groups=groups,
        default_group=data.get("default_group", ""),
        watch_interval=data.get("watch_interval", 5),
        source_path=str(path),
        drones=drones,
        queen=queen,
        playbooks=playbooks,
        notifications=notifications,
        coordination=coordination,
        jira=jira,
        test=test,
        terminal=terminal,
        resources=resources,
        workflows=workflows,
        tool_buttons=tool_buttons,
        action_buttons=action_buttons,
        task_buttons=task_buttons,
        custom_llms=custom_llms,
        provider_overrides=provider_overrides,
        log_level=data.get("log_level", "WARNING"),
        log_file=data.get("log_file"),
        port=data.get("port", 9090),
        daemon_url=data.get("daemon_url"),
        api_password=data.get("api_password"),
        graph_client_id=graph_data.get("client_id", ""),
        graph_tenant_id=graph_data.get("tenant_id", "common"),
        graph_client_secret=graph_data.get("client_secret", ""),
        trust_proxy=data.get("trust_proxy", False),
        tunnel_domain=data.get("tunnel_domain", ""),
        domain=data.get("domain", ""),
    )


def _auto_detect_config() -> HiveConfig:
    """Auto-detect git repos in ~/projects/ as workers."""
    projects_dir = Path.home() / "projects"
    projects = discover_projects(projects_dir)
    workers = [WorkerConfig(name=name, path=path) for name, path in projects]

    return HiveConfig(
        workers=workers,
        groups=[GroupConfig(name="all", workers=[w.name for w in workers])],
    )


def discover_projects(scan_dir: Path) -> list[tuple[str, str]]:
    """Scan a directory for git repos (up to 2 levels deep). Returns list of (name, path) tuples."""
    projects: list[tuple[str, str]] = []
    if not scan_dir.is_dir():
        return projects
    for child in sorted(scan_dir.iterdir()):
        if not child.is_dir():
            continue
        if (child / ".git").exists():
            projects.append((child.name, str(child)))
        else:
            # Check one level deeper (e.g. ~/projects/personal/<repo>)
            for grandchild in sorted(child.iterdir()):
                if grandchild.is_dir() and (grandchild / ".git").exists():
                    projects.append((grandchild.name, str(grandchild)))
    return projects


def _builtin_provider_defaults() -> dict[str, Any]:
    """Return built-in provider detection defaults for inclusion in generated configs."""
    from swarm.providers import list_builtin_providers

    overrides: dict[str, Any] = {}
    for bp in list_builtin_providers():
        defs = bp.get("defaults")
        if isinstance(defs, dict) and defs:
            overrides[str(bp["name"])] = dict(defs)
    return overrides


def write_config(
    output_path: str,
    workers: list[tuple[str, str]],
    groups: dict[str, list[str]],
    projects_dir: str,
    api_password: str | None = None,
    domain: str = "",
    ported_settings: dict[str, Any] | None = None,
    extra_settings: dict[str, Any] | None = None,
) -> None:
    """Write a swarm.yaml config file.

    Args:
        ported_settings: Optional dict of settings to carry over from a
            previous config (e.g. queen, drones, notifications, port).
            Workers and groups come from the scan, not from ported settings.
        extra_settings: Additional top-level keys to include (e.g. trust_proxy).
    """
    data: dict[str, Any] = {
        "session_name": "swarm",
        "projects_dir": projects_dir,
        "workers": [{"name": name, "path": path} for name, path in workers],
        "groups": [{"name": gname, "workers": members} for gname, members in groups.items()],
    }
    if ported_settings:
        # Merge ported settings, but never override workers/groups/projects_dir
        skip_keys = {"workers", "groups", "projects_dir", "session_name"}
        for key, value in ported_settings.items():
            if key not in skip_keys:
                data[key] = value
    if api_password:
        data["api_password"] = api_password
    if domain:
        data["domain"] = domain
    if extra_settings:
        data.update(extra_settings)
    # Include built-in provider defaults so users can see and tune them
    if "provider_overrides" not in data:
        data["provider_overrides"] = _builtin_provider_defaults()
    import tempfile

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    content = "# Generated by swarm init\n" + yaml.dump(
        data, default_flow_style=False, sort_keys=False
    )
    # Atomic write: write to temp file then rename (prevents partial writes on crash)
    fd, tmp = tempfile.mkstemp(dir=str(out.parent), suffix=".tmp")
    closed = False
    try:
        os.write(fd, content.encode())
        os.fchmod(fd, 0o600)
        os.close(fd)
        closed = True
        os.replace(tmp, str(out))
    except BaseException:
        if not closed:
            os.close(fd)
        Path(tmp).unlink(missing_ok=True)
        raise
