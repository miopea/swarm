"""Dataclasses and constants for hive configuration."""

from __future__ import annotations

import functools
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_log = logging.getLogger("swarm.config")


class ConfigError(Exception):
    """Raised when swarm.yaml is invalid."""


@dataclass
class DroneApprovalRule:
    """A pattern->action rule for drone choice menu handling."""

    pattern: str  # regex matched against choice menu text
    action: str = "approve"  # "approve" or "escalate"
    compiled: re.Pattern[str] = field(init=False, repr=False, compare=False)
    # Set by ``__post_init__`` when the supplied pattern fails to compile.
    # Validation reads this instead of re-running ``re.compile`` so a single
    # bad pattern doesn't trigger a second compile pass at validate-time.
    compile_error: str | None = field(init=False, repr=False, compare=False, default=None)

    def __post_init__(self) -> None:
        try:
            self.compiled = re.compile(self.pattern, re.IGNORECASE | re.MULTILINE)
        except re.error as exc:
            _log.warning("Invalid regex pattern %r in approval rule, ignoring", self.pattern)
            # Compile a never-matching regex so validation
            # can report the error without crashing at parse time.
            self.compiled = re.compile(r"(?!)")  # always fails
            self.compile_error = str(exc)


@dataclass
class ProviderTuning:
    """Per-provider tuning knobs for state detection and approval handling."""

    idle_pattern: str = ""  # regex -> RESTING
    busy_pattern: str = ""  # regex -> BUZZING
    choice_pattern: str = ""  # regex -> WAITING (choice prompt)
    user_question_pattern: str = ""  # regex -> never auto-approve
    safe_patterns: str = ""  # regex for auto-approvable tools
    approval_key: str = ""  # e.g. "y\r" or "\r"
    rejection_key: str = ""  # e.g. "n\r" or "\x1b"
    env_strip_prefixes: list[str] = field(default_factory=list)
    env_vars: dict[str, str] = field(default_factory=dict)
    tail_lines: int = 0  # 0 = provider default (30)
    # Pre-compiled patterns -- set in __post_init__
    _idle_re: re.Pattern[str] | None = field(init=False, repr=False, compare=False, default=None)
    _busy_re: re.Pattern[str] | None = field(init=False, repr=False, compare=False, default=None)
    _choice_re: re.Pattern[str] | None = field(init=False, repr=False, compare=False, default=None)
    _user_question_re: re.Pattern[str] | None = field(
        init=False, repr=False, compare=False, default=None
    )
    _safe_re: re.Pattern[str] | None = field(init=False, repr=False, compare=False, default=None)

    def __post_init__(self) -> None:
        for attr, src in (
            ("_idle_re", self.idle_pattern),
            ("_busy_re", self.busy_pattern),
            ("_choice_re", self.choice_pattern),
            ("_user_question_re", self.user_question_pattern),
            ("_safe_re", self.safe_patterns),
        ):
            if src:
                try:
                    object.__setattr__(self, attr, re.compile(src, re.IGNORECASE | re.MULTILINE))
                except re.error:
                    object.__setattr__(self, attr, re.compile(r"(?!)"))  # never-match

    def has_tuning(self) -> bool:
        """Return True if any tuning field is non-empty/non-zero."""
        return bool(
            self.idle_pattern
            or self.busy_pattern
            or self.choice_pattern
            or self.user_question_pattern
            or self.safe_patterns
            or self.approval_key
            or self.rejection_key
            or self.env_strip_prefixes
            or self.env_vars
            or self.tail_lines
        )


@dataclass
class StateThresholds:
    """Tunable thresholds for worker state detection hysteresis."""

    buzzing_confirm_count: int = 12  # consecutive readings before BUZZING -> RESTING
    stung_confirm_count: int = 2  # consecutive readings before -> STUNG
    revive_grace: float = 15.0  # seconds grace after revive (ignore STUNG)


@dataclass
class DroneConfig:
    """Background drones settings (``drones:`` section in swarm.yaml)."""

    enabled: bool = True
    escalation_threshold: float = 120.0
    poll_interval: float = 5.0
    # State-aware polling: override base interval for specific worker states.
    # Defaults derive from poll_interval if not set explicitly.
    poll_interval_buzzing: float = 0.0  # 0 = 2x poll_interval
    poll_interval_waiting: float = 0.0  # 0 = poll_interval (fast -- prompt needs response)
    poll_interval_resting: float = 0.0  # 0 = 3x poll_interval
    auto_approve_yn: bool = False
    max_revive_attempts: int = 3
    max_poll_failures: int = 5
    max_idle_interval: float = 30.0
    auto_stop_on_complete: bool = True
    auto_approve_assignments: bool = True
    idle_assign_threshold: int = 3
    auto_complete_min_idle: float = 45.0  # seconds idle before proposing task completion
    sleeping_poll_interval: float = 30.0  # full poll interval for sleeping workers
    sleeping_threshold: float = 900.0  # seconds idle before RESTING -> SLEEPING
    stung_reap_timeout: float = 30.0  # seconds before STUNG workers are auto-removed
    state_thresholds: StateThresholds = field(default_factory=StateThresholds)
    approval_rules: list[DroneApprovalRule] = field(default_factory=list)
    # Directory prefixes that are always safe to read from (e.g. "~/.swarm/uploads/").
    # Read operations matching these paths are auto-approved regardless of approval_rules.
    allowed_read_paths: list[str] = field(default_factory=list)
    # Context window awareness: warn at this percentage (0.0-1.0), 0 = disabled
    context_warning_threshold: float = 0.7
    context_critical_threshold: float = 0.9
    # Speculative task prep: disabled by default, opt-in per swarm.yaml
    speculation_enabled: bool = False
    # Idle-watcher drone (task #225 Phase 2): nudge RESTING/SLEEPING workers
    # that have an ASSIGNED/IN_PROGRESS task but aren't actually working on
    # it.  0 disables.  ``idle_nudge_debounce_seconds`` suppresses repeat
    # nudges for the same (worker, task) pair so a stuck worker doesn't
    # get spammed.
    idle_nudge_interval_seconds: float = 180.0
    idle_nudge_debounce_seconds: float = 900.0
    # Task #546: after this many consecutive no-progress nudges (the
    # worker's state + outstanding-work fingerprint unchanged between
    # nudges), the watcher STOPS poking and escalates once to the
    # operator instead of looping forever on a task the worker can't
    # progress (e.g. a shipped fix awaiting operator verification).
    # Applies to both the idle-watcher and the inter-worker watcher.
    # 0 disables the cap → pre-#546 unbounded re-nudging.
    idle_nudge_max_repeats: int = 3
    # Native /goal seeding: at task dispatch, translate the task's
    # acceptance_criteria into a native ``/goal`` condition on providers
    # whose CLI supports it (Claude Code, Codex). The provider's own
    # evaluator then runs the keep-working loop — Swarm builds no
    # evaluator. ``native_goal_max_turns`` is the runaway bound baked
    # into the condition string ("...or stop after N turns...").
    native_goal_enabled: bool = True
    native_goal_max_turns: int = 25
    # Plan-mode gate for user-request tasks: when True (default), the
    # dispatch path prepends a plan-mode preamble to tasks originating
    # from Jira sync, email import, or the operator dashboard (i.e.
    # ``SwarmTask.source_worker`` empty). The worker investigates
    # read-only, presents a plan via Claude Code's ExitPlanMode, and
    # parks in WAITING until the operator approves from the dashboard.
    # Worker-to-worker handoffs (``source_worker`` set) always bypass
    # — that peer already reasoned about the work. Set False to revert
    # to the legacy fire-and-forget dispatch behavior for all tasks.
    user_request_plan_mode: bool = True
    # Auto-assign project-affinity floor (task #341): when neither the
    # deterministic project-affinity scorer nor the headless Queen reach
    # this confidence on a task, the assigner parks the task in backlog
    # rather than force-fitting it to whichever worker scored highest.
    # Range 0.0-1.0; 0.0 disables the floor and reverts to legacy behavior.
    assign_affinity_floor: float = 0.5
    # Auto-assign operator-engagement window (task #341): when a task has
    # no unambiguous project signal, prefer the worker whose PTY the
    # operator has typed in within this many minutes. 0 disables.
    assign_operator_engagement_minutes: float = 10.0
    # Dreamer drone: periodic pattern-mining sweep over the buzz log that
    # auto-curates ``queen_learnings`` rows tagged
    # ``discovered_by_dreamer:{key}``. Runs every
    # ``dreamer_interval_seconds`` (0 disables); looks back
    # ``dreamer_lookback_hours``; promotes a cluster to a learning when it
    # crosses ``dreamer_min_pattern_count`` AND involves at least 2
    # distinct workers (single-worker chatter doesn't mint patterns).
    dreamer_interval_seconds: float = 14400.0  # 4h
    dreamer_lookback_hours: float = 24.0
    dreamer_min_pattern_count: int = 3


@dataclass
class OversightConfig:
    """Queen oversight settings (``queen.oversight:`` section in swarm.yaml)."""

    enabled: bool = True
    buzzing_threshold_minutes: float = 15.0
    drift_check_interval_minutes: float = 10.0
    max_calls_per_hour: int = 6
    # Hard precondition gate: when the operator has typed in a worker's PTY
    # within this window, downgrade `major: redirect` interventions to `note`
    # (or skip entirely) so a periodic drift signal can't interrupt an
    # interactive session. Set to 0 to disable the gate.
    operator_engagement_minutes: float = 10.0
    # Auto-park: after this many consecutive drift checks with NO task
    # progress (task.updated_at frozen while ACTIVE), raise ONE park
    # proposal instead of intervening again — the operator-blocked-stall
    # guard (a task waiting on the operator must not churn forever).
    # auto_park_enabled=False disables. After a rejected park proposal,
    # don't re-propose for the same (worker,task) for the backoff window.
    auto_park_enabled: bool = True
    auto_park_no_progress_checks: int = 3
    auto_park_reject_backoff_seconds: float = 7200.0


@dataclass
class ResourceConfig:
    """System resource monitoring (``resources:`` section in swarm.yaml)."""

    enabled: bool = True
    poll_interval: float = 10.0  # seconds between snapshots
    # Swap thresholds only trigger worker suspension when memory is also
    # strained (see classify_pressure in swarm.resources.monitor).  Swap alone
    # is normal Linux cold-page behaviour and should not cause suspension.
    elevated_swap_pct: float = 40.0  # swap % -> ELEVATED warning
    elevated_mem_pct: float = 80.0  # mem % -> ELEVATED warning
    high_swap_pct: float = 70.0  # swap % -> HIGH (only if mem also >= elevated_mem_pct)
    high_mem_pct: float = 90.0  # mem % -> HIGH on its own
    critical_swap_pct: float = 85.0  # swap % -> CRITICAL (only if mem also >= high_mem_pct)
    critical_mem_pct: float = 95.0  # mem % -> CRITICAL on its own
    suspend_on_high: bool = True  # auto-suspend workers at HIGH
    dstate_scan: bool = True  # scan for D-state descendants
    dstate_threshold_sec: float = 120.0  # D-state age before alerting


@dataclass
class QueenConfig:
    """Queen conductor settings (``queen:`` section in swarm.yaml)."""

    cooldown: float = 30.0
    enabled: bool = True
    # Headless-decision prompt: prepended to the headless ``claude -p``
    # coordinator path in ``swarm.queen.queen`` used by drone auto-assign,
    # oversight, hive coordination, and QueenAnalyzer.analyze_worker.
    # NOT the interactive Queen's role — that lives in
    # ``~/.swarm/queen/workdir/CLAUDE.md`` (seeded from
    # ``QUEEN_SYSTEM_PROMPT`` in ``swarm.queen.runtime``).
    # Default empty; the daemon seeds ``HEADLESS_DECISION_PROMPT``
    # (from ``swarm.queen.queen``) when this is unset so fresh installs
    # and cleared-field deployments still frame the role. Any non-empty
    # value here overrides the seed.
    system_prompt: str = ""
    min_confidence: float = 0.7
    max_session_calls: int = 20
    max_session_age: float = 1800.0  # 30 minutes
    auto_assign_tasks: bool = True
    oversight: OversightConfig = field(default_factory=OversightConfig)


@dataclass
class PlaybookConfig:
    """Playbook-synthesis-loop settings (``playbooks:`` section).

    Phase 1 uses ``enabled`` / ``eligible_task_types`` /
    ``min_resolution_chars`` / ``max_synth_per_hour`` to gate headless-
    Queen synthesis volume. The promote/prune/consolidation knobs are
    declared here for later phases so no further config migration is
    needed. See ``docs/specs/playbook-synthesis-loop.md``.
    """

    enabled: bool = True
    eligible_task_types: list[str] = field(default_factory=lambda: ["feature", "bug", "chore"])
    min_resolution_chars: int = 80
    max_synth_per_hour: int = 20
    auto_promote_uses: int = 3
    auto_promote_winrate: float = 0.7
    prune_min_uses: int = 5
    prune_max_winrate: float = 0.3
    consolidation_interval_seconds: float = 21600.0  # 6h
    dedupe_similarity_threshold: float = 0.75
    install_as_native_skills: bool = True


@dataclass
class CoordinationConfig:
    """Cross-worker coordination (``coordination:`` section in swarm.yaml)."""

    mode: str = "single-branch"  # "single-branch" | "worktree"
    auto_pull: bool = True
    file_ownership: str = "warning"  # "off" | "warning" | "hard-block"


@dataclass
class JiraConfig:
    """Jira integration settings (``jira:`` section in swarm.yaml).

    Authentication is via OAuth 2.0 (3LO) only.  Configure ``client_id``
    and ``client_secret``, then connect from the Config page.
    """

    enabled: bool = False
    project: str = ""  # e.g. "PROJ"
    sync_interval_minutes: float = 5.0
    import_filter: str = ""  # JQL filter for importing tickets
    import_label: str = ""  # Jira label to filter imports (e.g. "swarm"); empty = all
    lookback_days: int = 30  # How far back to look for issues (0 = no limit)
    status_map: dict[str, str] = field(
        default_factory=lambda: {
            "backlog": "To Do",
            "unassigned": "To Do",
            "assigned": "To Do",
            "active": "In Progress",
            "done": "Done",
            "failed": "To Do",
        }
    )

    client_id: str = ""  # Atlassian OAuth app client ID
    client_secret: str = ""  # Atlassian OAuth app client secret (or $ENV_VAR)
    cloud_id: str = ""  # Auto-discovered Jira Cloud site ID

    def resolved_client_secret(self) -> str:
        """Resolve client_secret, expanding $ENV_VAR references."""
        if self.client_secret.startswith("$"):
            return os.environ.get(self.client_secret[1:], "")
        return self.client_secret


@dataclass
class WebhookConfig:
    """Webhook notification backend (``notifications.webhook:`` section in swarm.yaml)."""

    url: str = ""
    events: list[str] = field(default_factory=list)  # empty = all events


@dataclass
class EmailConfig:
    """Email notification backend (``notifications.email:`` section in swarm.yaml)."""

    enabled: bool = False
    smtp_host: str = "localhost"
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    use_tls: bool = True
    from_address: str = ""
    to_addresses: list[str] = field(default_factory=list)
    events: list[str] = field(default_factory=list)  # empty = all events


@dataclass
class NotifyConfig:
    """Notification settings (``notifications:`` section in swarm.yaml)."""

    terminal_bell: bool = True
    desktop: bool = True
    desktop_events: list[str] = field(default_factory=list)  # empty = all events
    terminal_events: list[str] = field(default_factory=list)  # empty = all events
    debounce_seconds: float = 5.0
    templates: dict[str, str] = field(default_factory=dict)  # event_type → message template
    webhook: WebhookConfig = field(default_factory=WebhookConfig)
    email: EmailConfig = field(default_factory=EmailConfig)


@dataclass
class ToolButtonConfig:
    """A configurable tool button (``tool_buttons:`` section in swarm.yaml)."""

    label: str
    command: str


@dataclass
class ActionButtonConfig:
    """A unified action button for the dashboard action bar.

    Replaces the hardcoded built-in buttons + separate ``tool_buttons`` with a
    single reorderable, visibility-togglable list.
    """

    label: str
    action: str = ""  # built-in: revive, refresh, queen, kill; empty = custom
    command: str = ""  # text sent to worker (custom buttons; blank = continue)
    style: str = "secondary"  # CSS class suffix: secondary, queen, danger
    show_mobile: bool = True
    show_desktop: bool = True


DEFAULT_ACTION_BUTTONS: list[ActionButtonConfig] = [
    ActionButtonConfig(label="Revive", action="revive", style="secondary"),
    ActionButtonConfig(label="Refresh", action="refresh", style="secondary"),
    ActionButtonConfig(label="Ask Queen", action="queen", style="queen"),
    ActionButtonConfig(label="Kill", action="kill", style="danger"),
    ActionButtonConfig(
        label="Export",
        action="export",
        style="secondary",
        show_mobile=False,
    ),
]


@dataclass
class TaskButtonConfig:
    """A configurable task-list button (``task_buttons:`` section in swarm.yaml).

    Controls order and mobile/desktop visibility of task action buttons.
    Styles are derived from the action name (hardcoded CSS per action type).
    """

    label: str
    action: (
        str  # edit, assign, done, unassign, fail, reopen, approve, reject, log, retry_draft, remove
    )
    show_mobile: bool = True
    show_desktop: bool = True


DEFAULT_TASK_BUTTONS: list[TaskButtonConfig] = [
    TaskButtonConfig(label="Edit", action="edit"),
    TaskButtonConfig(label="Hand to Queen", action="promote"),
    TaskButtonConfig(label="Assign", action="assign"),
    TaskButtonConfig(label="Start", action="start"),
    TaskButtonConfig(label="Done", action="done"),
    TaskButtonConfig(label="Unassign", action="unassign"),
    TaskButtonConfig(label="Fail", action="fail"),
    TaskButtonConfig(label="Reopen", action="reopen"),
    TaskButtonConfig(label="Approve", action="approve"),
    TaskButtonConfig(label="Reject", action="reject"),
    TaskButtonConfig(label="Log", action="log"),
    TaskButtonConfig(label="Retry Draft", action="retry_draft"),
    TaskButtonConfig(label="\u00d7", action="remove"),
]


@dataclass
class CustomLLMConfig:
    """User-defined LLM provider (``llms:`` section in swarm.yaml)."""

    name: str  # unique identifier, used in dropdowns
    command: list[str]  # CLI command to launch worker, e.g. ["aider"]
    display_name: str = ""  # human label (defaults to name.title())
    tuning: ProviderTuning = field(default_factory=ProviderTuning)


def _validate_tuning_patterns(prefix: str, tuning: ProviderTuning) -> list[str]:
    """Validate regex patterns in a ProviderTuning, returning error messages."""
    errors: list[str] = []
    for field_name in (
        "idle_pattern",
        "busy_pattern",
        "choice_pattern",
        "user_question_pattern",
        "safe_patterns",
    ):
        val = getattr(tuning, field_name, "")
        if val:
            try:
                re.compile(val)
            except re.error as exc:
                errors.append(f"{prefix}.{field_name}: invalid regex '{val}': {exc}")
    return errors


@dataclass
class WorkerConfig:
    name: str
    path: str
    description: str = ""
    provider: str = ""  # empty = inherit HiveConfig.provider
    isolation: str = ""  # "" = shared, "worktree" = git worktree
    identity: str = ""  # path to worker identity markdown file (e.g. ~/.swarm/identities/api.md)
    approval_rules: list[DroneApprovalRule] = field(default_factory=list)
    allowed_tools: list[str] = field(default_factory=list)

    @functools.cached_property
    def resolved_path(self) -> Path:
        return Path(self.path).expanduser().resolve()

    def resolved_identity_path(self) -> Path | None:
        """Return the resolved identity file path, or None if not set."""
        if not self.identity:
            return None
        return Path(self.identity).expanduser().resolve()

    def load_identity(self) -> str:
        """Load identity file content, returning empty string if not found."""
        p = self.resolved_identity_path()
        if p is None or not p.is_file():
            return ""
        try:
            return p.read_text()
        except OSError:
            return ""


@dataclass
class GroupConfig:
    name: str
    workers: list[str]


@dataclass
class TestConfig:
    """Settings for ``swarm test`` supervised orchestration testing."""

    enabled: bool = False
    port: int = 9091  # dedicated test port (separate from main web UI)
    auto_resolve_delay: float = 4.0  # seconds before Queen resolves proposal
    report_dir: str = "~/.swarm/reports"
    auto_complete_min_idle: float = 10.0  # shorter idle threshold for test mode


@dataclass
class TerminalConfig:
    """Web terminal settings (pty -> xterm)."""

    replay_scrollback: bool = True
    # Deprecated: render_ansi() output is bounded by screen size; kept for
    # backwards-compatible config parsing only.
    replay_max_bytes: int = 0


@dataclass
class SandboxConfig:
    """Opt-in wiring for Claude Code's native sandbox mode.

    Disabled by default — the current PreToolUse approval flow keeps
    working until an operator turns this on. When enabled, the hooks
    installer detects the installed CC version and, if supported,
    merges ``settings["sandbox"] = settings_overrides`` into
    ``~/.claude/settings.json``. Unsupported versions get a warning
    and are left on the legacy approval path.
    """

    # Master switch. When False, no sandbox keys are written to
    # settings.json even if Claude Code supports them.
    enabled: bool = False
    # Minimum Claude Code version (dotted) that the installer should
    # accept before writing sandbox keys. Empty string disables the
    # version gate.
    min_claude_version: str = "2.0"
    # Passed through verbatim as ``settings["sandbox"]``. Schema varies
    # with CC version — consult the CC release notes for the exact
    # keys (allow_filesystem_writes, allow_network, denied_tools, etc.).
    settings_overrides: dict[str, Any] = field(default_factory=dict)


@dataclass
class HiveConfig:
    session_name: str = "swarm"
    projects_dir: str = "~/projects"
    provider: str = "claude"  # global default: "claude" | "gemini" | "codex"
    workers: list[WorkerConfig] = field(default_factory=list)
    groups: list[GroupConfig] = field(default_factory=list)
    default_group: str = ""
    watch_interval: int = 5
    source_path: str | None = None
    # Where this HiveConfig was loaded from — set by the loader, used
    # by the startup banner to tell the operator at a glance whether
    # the daemon is reading from the DB or silently falling back.
    # Values: "db" | "yaml" | "fresh" | "unknown".
    config_source: str = "unknown"
    drones: DroneConfig = field(default_factory=DroneConfig)
    queen: QueenConfig = field(default_factory=QueenConfig)
    playbooks: PlaybookConfig = field(default_factory=PlaybookConfig)
    notifications: NotifyConfig = field(default_factory=NotifyConfig)
    coordination: CoordinationConfig = field(default_factory=CoordinationConfig)
    jira: JiraConfig = field(default_factory=JiraConfig)
    test: TestConfig = field(default_factory=TestConfig)
    terminal: TerminalConfig = field(default_factory=TerminalConfig)
    resources: ResourceConfig = field(default_factory=ResourceConfig)
    sandbox: SandboxConfig = field(default_factory=SandboxConfig)
    # Skill overrides per task type (e.g. {"bug": "/fix-and-ship", "feature": "/feature"}).
    # Keys are TaskType values: bug, feature, verify, chore.
    # Set a value to null/empty to disable skill invocation for that type.
    workflows: dict[str, str] = field(default_factory=dict)
    tool_buttons: list[ToolButtonConfig] = field(default_factory=list)
    action_buttons: list[ActionButtonConfig] = field(default_factory=list)
    task_buttons: list[TaskButtonConfig] = field(default_factory=list)
    custom_llms: list[CustomLLMConfig] = field(default_factory=list)
    provider_overrides: dict[str, ProviderTuning] = field(default_factory=dict)
    log_level: str = "WARNING"
    log_file: str | None = None
    port: int = 9090  # web UI / API server port
    daemon_url: str | None = None  # e.g. "http://localhost:9090" -- dashboard connects via API
    api_password: str | None = None  # password for web UI config-mutating endpoints
    graph_client_id: str = ""  # Azure AD app client ID for Microsoft Graph
    graph_tenant_id: str = "common"  # Azure AD tenant ID (or "common")
    graph_client_secret: str = ""  # Azure AD client secret (required for web app OAuth)
    trust_proxy: bool = False  # trust X-Forwarded-For header (enable behind a reverse proxy)
    tunnel_domain: str = ""  # custom domain for named Cloudflare tunnels (advanced)
    domain: str = ""  # public domain for WebAuthn RP ID (e.g. swarm.example.com)

    def get_group(self, name: str) -> list[WorkerConfig]:
        name_lower = name.lower()
        for g in self.groups:
            if g.name.lower() == name_lower:
                members = {m.lower() for m in g.workers}
                return [w for w in self.workers if w.name.lower() in members]
        raise ValueError(f"Unknown group: {name}")

    def get_worker(self, name: str) -> WorkerConfig | None:
        name_lower = name.lower()
        for w in self.workers:
            if w.name.lower() == name_lower:
                return w
        return None

    def validate(self) -> list[str]:
        """Validate config, returning a list of error messages (empty = valid)."""
        from swarm.config.validation import validate_config

        return validate_config(self)

    def apply_env_overrides(self) -> None:
        """Apply environment variable overrides."""

        if val := os.environ.get("SWARM_SESSION_NAME"):
            self.session_name = val
        if val := os.environ.get("SWARM_WATCH_INTERVAL"):
            try:
                self.watch_interval = int(val)
            except ValueError:
                _log.warning("invalid SWARM_WATCH_INTERVAL=%r, ignoring", val)
        if val := os.environ.get("SWARM_DAEMON_URL"):
            self.daemon_url = val
        if val := os.environ.get("SWARM_API_PASSWORD"):
            self.api_password = val
        if val := os.environ.get("SWARM_PORT"):
            try:
                self.port = int(val)
            except ValueError:
                _log.warning("invalid SWARM_PORT=%r, ignoring", val)
