"""SwarmDaemon — long-running backend service."""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypedDict

from aiohttp import web

if TYPE_CHECKING:
    from swarm.auth.graph import GraphTokenManager as GraphManager
    from swarm.auth.jira import JiraTokenManager
    from swarm.pty.provider import WorkerProcessProvider
    from swarm.queen.oversight import OversightResult, OversightSignal
    from swarm.update import UpdateResult

from swarm.config import HiveConfig, WorkerConfig
from swarm.drones.log import DroneLog, LogCategory, SystemAction, SystemEntry
from swarm.drones.pilot import DronePilot
from swarm.events import EventEmitter
from swarm.logging import get_logger
from swarm.notify.bus import NotificationBus
from swarm.notify.desktop import desktop_backend
from swarm.notify.terminal import terminal_bell_backend
from swarm.queen.queen import Queen
from swarm.queen.queue import QueenCallQueue
from swarm.server.analyzer import QueenAnalyzer
from swarm.server.broadcast import BroadcastHub
from swarm.server.config_manager import ConfigManager
from swarm.server.email_service import EmailService
from swarm.server.jira_service import JiraService
from swarm.server.loop_runner import BackgroundLoopRunner
from swarm.server.proposals import ProposalManager
from swarm.server.resource_monitor import ResourceMonitor
from swarm.server.task_manager import TaskManager
from swarm.server.test_runner import TestRunner
from swarm.server.worker_service import WorkerService
from swarm.tasks.board import TaskBoard
from swarm.tasks.history import TaskHistory
from swarm.tasks.proposal import (
    AssignmentProposal,
    ProposalStore,
)
from swarm.tasks.store import FileTaskStore
from swarm.tasks.task import (
    SwarmTask,
    TaskPriority,
    TaskStatus,
    TaskType,
)
from swarm.tunnel import TunnelManager, TunnelState
from swarm.worker.worker import Worker, WorkerState

_log = get_logger("server.daemon")

_QUEEN_MAX_CONCURRENT = 2
_USAGE_REFRESH_INTERVAL = 10  # seconds
_HEARTBEAT_INITIAL_DELAY = 2  # seconds
_HEARTBEAT_INTERVAL = 8  # seconds
_UPDATE_CHECK_DELAY = 5  # seconds
_USAGE_CONCURRENCY = 20  # max concurrent to_thread calls for usage refresh


# --- Exception classes ---


class ConflictEntry(TypedDict):
    """A file-level conflict between two or more workers."""

    file: str
    workers: list[str]


class SwarmOperationError(Exception):
    """Base for daemon operation errors."""


class WorkerNotFoundError(SwarmOperationError):
    """Referenced worker does not exist."""


class TaskOperationError(SwarmOperationError):
    """Task op failed (not found, wrong state).

    Carries an HTTP ``status_code`` (default 404) so the API layer
    can return 409 for wrong-state errors vs 404 for not-found.
    """

    def __init__(self, message: str, *, status_code: int = 404) -> None:
        super().__init__(message)
        self.status_code = status_code


class SwarmDaemon(EventEmitter):
    """Long-running backend service for the swarm."""

    def __init__(self, config: HiveConfig, *, task_store: FileTaskStore | None = None) -> None:
        self.__init_emitter__()
        self.config = config
        # Diagnostic: anchor the in-memory workflows state at daemon
        # construction.  Triages "config reverts on restart" symptoms by
        # confirming whether the loader handed us the right value
        # (Amanda 2026-05-05 — DB + load_config_from_db both verify
        # correct, but the daemon's pre.cfg.workflows reads as {}).
        # WARNING level — the 2026.5.5.15 INFO version was silently
        # missing from her logs, suggesting a log-level / timing
        # issue.  WARNING is on by default, can't be filtered out.
        _log.warning("daemon init: config.workflows=%r", config.workflows)
        # Daemon-boot timestamp — used by the IdleWatcher's MCP tools-stale
        # detection (task #257) to reason about "did this worker make any
        # MCP calls since the daemon last restarted?".  Re-stamped on
        # every ``__init__`` so ``os.execv`` reloads get a fresh value.
        self.daemon_start_time: float = time.time()
        # Register custom LLM providers before any workers or queen init
        if config.custom_llms:
            from swarm.providers import register_custom_providers

            register_custom_providers(config.custom_llms)
        if config.provider_overrides:
            from swarm.providers import register_provider_overrides

            register_provider_overrides(config.provider_overrides)
        self.workers: list[Worker] = []
        self.pool: WorkerProcessProvider | None = None
        self._prev_worker_costs: dict[str, float] = {}
        # File lock registry: path → (worker_name, timestamp)
        self.file_locks: dict[str, tuple[str, float]] = {}
        self._file_lock_ttl: float = 60.0  # seconds

        # --- Unified SQLite storage ---
        from swarm.db import SqliteTaskHistory, SqliteTaskStore, SwarmDB
        from swarm.db.migrate import auto_migrate

        self.swarm_db = SwarmDB()
        auto_migrate(self.swarm_db)
        from swarm.messages.store import MessageStore

        self.message_store = MessageStore(swarm_db=self.swarm_db)
        from swarm.tasks.blockers import BlockerStore

        self.blocker_store = BlockerStore(self.swarm_db)

        self._worker_lock = asyncio.Lock()
        # Persistence: tasks and system log survive restarts
        _task_store = task_store or SqliteTaskStore(self.swarm_db)

        from swarm.db.buzz_store import BuzzStore

        _buzz_store = BuzzStore(self.swarm_db)
        self.drone_log = DroneLog(buzz_store=_buzz_store)
        self.task_board = TaskBoard(store=_task_store)
        self.task_history: TaskHistory | SqliteTaskHistory = SqliteTaskHistory(self.swarm_db)

        from swarm.db.queen_chat_store import QueenChatStore

        self.queen_chat = QueenChatStore(self.swarm_db)

        from swarm.db.playbook_store import PlaybookStore

        self.playbook_store = PlaybookStore(self.swarm_db)

        from swarm.db.pipeline_store import SqlitePipelineStore
        from swarm.pipelines.engine import PipelineEngine
        from swarm.services.registry import ServiceRegistry

        self.service_registry = ServiceRegistry()

        from swarm.services.handlers import register_defaults

        register_defaults(self.service_registry)

        self.pipeline_engine = PipelineEngine(
            store=SqlitePipelineStore(self.swarm_db),
            task_board=self.task_board,
            service_registry=self.service_registry,
        )
        from swarm.providers import get_provider
        from swarm.queen.queen import HEADLESS_DECISION_PROMPT

        # Seed the headless-decision prompt if unset. Covers fresh installs and
        # existing deployments that cleared the field during the task #251
        # interactive-mode migration. Operator override still wins: any
        # non-empty value in the DB (or swarm.yaml) bypasses the seed.
        if not config.queen.system_prompt:
            config.queen.system_prompt = HEADLESS_DECISION_PROMPT
        self.queen = Queen(
            config=config.queen,
            session_name=config.session_name,
            provider=get_provider(config.provider),
        )
        # Playbook synthesis (docs/specs/playbook-synthesis-loop.md): the
        # headless Queen mines successful completions into reusable
        # procedural memory. Fired post-ship from complete_task().
        from swarm.playbooks.synthesizer import PlaybookSynthesizer

        self.playbook_synthesizer = PlaybookSynthesizer(
            queen=self.queen,
            store=self.playbook_store,
            config=config.playbooks,
            drone_log=self.drone_log,
        )
        from swarm.playbooks.consolidator import PlaybookConsolidator

        self.playbook_consolidator = PlaybookConsolidator(
            queen=self.queen,
            store=self.playbook_store,
            drone_log=self.drone_log,
        )
        self.queen_queue = QueenCallQueue(
            max_concurrent=_QUEEN_MAX_CONCURRENT,
            on_status_change=self._on_queen_queue_status_change,
            get_worker_state=self._get_worker_state,
        )
        from swarm.db import SqliteProposalStore

        self.proposal_store: ProposalStore | SqliteProposalStore = SqliteProposalStore(
            self.swarm_db
        )
        self.notification_bus = self._build_notification_bus(config)
        self.proposals = ProposalManager(
            store=self.proposal_store,
            broadcast_ws=self.broadcast_ws,
            drone_log=self.drone_log,
            notification_bus=self.notification_bus,
            task_board=self.task_board,
            get_worker=self.get_worker,
            get_workers=lambda: self.workers,
            get_pilot=lambda: self.pilot,
            assign_task=self.assign_and_start_task,
            complete_task=self.complete_task,
            execute_escalation=lambda p: self.analyzer.execute_escalation(p),
        )
        self.analyzer = QueenAnalyzer(
            queen=self.queen,
            queue=self.queen_queue,
            broadcast_ws=self.broadcast_ws,
            drone_log=self.drone_log,
            emit_event=self.emit,
            proposal_store=self.proposal_store,
            queue_proposal=self.queue_proposal,
            task_board=self.task_board,
            get_worker=self.get_worker,
            require_worker=self._require_worker,
            get_workers=lambda: self.workers,
            get_pool=lambda: self.pool,
            get_config=lambda: self.config,
            get_worker_descriptions=self._worker_descriptions,
            clear_escalation=lambda name: self.pilot.clear_escalation(name) if self.pilot else None,
            record_completion_verdict=self._record_completion_verdict,
            is_focused=self.proposals.is_focused,
        )
        # Apply workflow skill overrides from config
        if config.workflows:
            from swarm.tasks.workflows import apply_config_overrides

            apply_config_overrides(config.workflows)
        # Coordination: file ownership + auto-pull sync
        from swarm.coordination.ownership import FileOwnershipMap, OwnershipMode
        from swarm.coordination.sync import AutoPullSync

        coord = config.coordination
        try:
            ownership_mode = OwnershipMode(coord.file_ownership)
        except ValueError:
            ownership_mode = OwnershipMode.WARNING
        self.file_ownership = FileOwnershipMap(mode=ownership_mode)
        self.auto_pull = AutoPullSync(enabled=coord.auto_pull)
        # Jira integration
        from swarm.integrations.jira import JiraSyncService

        self.jira_mgr = self._build_jira_token_manager(config)
        self.jira = JiraSyncService(config.jira, token_manager=self.jira_mgr)
        self._jira_auth_pending: dict[str, str] = {}  # state → csrf token
        self.pilot: DronePilot | None = None
        # --- BroadcastHub: WebSocket client management and debounced broadcasts ---
        self.hub = BroadcastHub(track_task=self._track_task)
        # --- BackgroundLoopRunner: owns the periodic-loop task lifecycle ---
        # Loops are registered in start() once the daemon is fully wired,
        # since several depend on subsystems that aren't constructed yet
        # (test_runner, config_mgr, etc.).
        self.loop_runner = BackgroundLoopRunner()
        self._conflicts: list[ConflictEntry] = []
        self._heartbeat_snapshot: dict[str, str] = {}
        # In-flight Queen analysis tracking lives on self.analyzer
        self.start_time = time.time()
        self._bg_tasks: set[asyncio.Task[object]] = set()
        # --- EscalationHandler: escalation, oversight, notifications ---
        from swarm.server.escalation_handler import EscalationHandler

        self.escalation = EscalationHandler(
            broadcast_ws=self.broadcast_ws,
            notification_bus=self.notification_bus,
            proposal_store=self.proposal_store,
            get_analyzer=lambda: self.analyzer,
            get_queen=lambda: self.queen,
            emit=self.emit,
        )
        # --- StatePublisher: owns state/task/pipeline broadcasting ---
        from swarm.server.state_publisher import StatePublisher

        self.publisher = StatePublisher(
            broadcast_ws=self.broadcast_ws,
            get_workers=lambda: self.workers,
            get_worker_task_map=self._worker_task_map,
            expire_proposals=self._expire_stale_proposals,
            broadcast_proposals=self._broadcast_proposals,
            clear_worker_inflight=lambda name: self.analyzer.clear_worker_inflight(name),
            pending_for_worker=self.proposal_store.pending_for_worker,
            clear_resolved_proposals=self.proposal_store.clear_resolved,
            update_proposal_status=self.proposal_store.update_status,
            push_notification=lambda **kw: self.push_notification(**kw),
            notification_bus=self.notification_bus,
            drone_log=self.drone_log,
            emit=self.emit,
            get_pressure_level=lambda: getattr(self, "_prev_pressure_level", "nominal"),
            pipeline_engine=self.pipeline_engine,
            service_registry=self.service_registry,
            track_task=self._track_task,
            mark_dirty=lambda: self._mark_state_dirty(),
        )
        # --- ProposalCoordinator: task-done, assignment, proposal lifecycle ---
        from swarm.server.proposal_coordinator import ProposalCoordinator

        self.proposal_coord = ProposalCoordinator(
            proposals=self.proposals,
            proposal_store=self.proposal_store,
            get_analyzer=lambda: self.analyzer,
            get_queen=lambda: self.queen,
            broadcast_ws=self.broadcast_ws,
            notification_bus=self.notification_bus,
            get_pilot=lambda: self.pilot,
            assign_task=self.assign_and_start_task,
            track_task=self._track_task,
            emit=self.emit,
        )
        # Resource monitoring (lifecycle owned by loop_runner)
        self.resource_mon = ResourceMonitor(
            broadcast_ws=self.broadcast_ws,
            get_pilot=lambda: self.pilot,
            get_pool=lambda: self.pool,
            get_workers=lambda: self.workers,
            get_resource_config=lambda: self.config.resources,
            notification_bus=lambda: self.notification_bus,
        )
        # Microsoft Graph OAuth
        self.graph_mgr = self._build_graph_manager(config)
        self._graph_auth_pending: dict[str, str] = {}  # state → code_verifier
        # Email service (attachments, draft replies, email processing)
        self.email = EmailService(
            drone_log=self.drone_log,
            queen=self.queen,
            graph_mgr=self.graph_mgr,
            broadcast_ws=self.broadcast_ws,
        )
        # Task lifecycle manager (create, edit, status transitions)
        self.tasks = TaskManager(
            task_board=self.task_board,
            task_history=self.task_history,
            drone_log=self.drone_log,
            notification_bus=self.notification_bus,
        )
        self.config_mgr = ConfigManager(
            config=self.config,
            broadcast_ws=self.broadcast_ws,
            drone_log=self.drone_log,
            apply_config=self.apply_config,
            get_pilot=lambda: self.pilot,
            rebuild_graph=self._rebuild_graph,
            rebuild_jira=self._rebuild_jira,
            get_worker_svc=lambda: self.worker_svc,
            swarm_db=self.swarm_db,
        )
        self.worker_svc = WorkerService(
            broadcast_ws=self.broadcast_ws,
            drone_log=self.drone_log,
            task_board=self.task_board,
            get_pilot=lambda: self.pilot,
            get_pool=lambda: self.pool,
            get_config=lambda: self.config,
            get_workers=lambda: self.workers,
            set_workers=lambda ws: setattr(self, "workers", ws),
            worker_lock=self._worker_lock,
            init_pilot=lambda enabled: self.init_pilot(enabled=enabled),
        )
        self.tunnel = TunnelManager(
            port=config.port,
            on_state_change=self._on_tunnel_state_change,
        )
        # --- JiraService: Jira import/export/sync ---
        self.jira_svc = JiraService(
            get_jira=lambda: self.jira,
            task_board=self.task_board,
            broadcast_ws=self.broadcast_ws,
            drone_log=self.drone_log,
            track_task=self._track_task,
            get_sync_interval=lambda: self.config.jira.sync_interval_minutes * 60,
        )
        # --- TestRunner: test mode lifecycle ---
        self.test_runner = TestRunner(
            daemon=self,
            task_board=self.task_board,
            broadcast_ws=self.broadcast_ws,
            track_task=self._track_task,
            create_task=self.create_task,
            get_pilot=lambda: self.pilot,
            emitter=self,
        )
        # Update detection (loop lifecycle owned by loop_runner)
        self._update_result: UpdateResult | None = None
        # Daemon lock FD — set externally by run_server()
        self._lock_fd: int | None = None
        # --- InvariantReconciler: task-board state-invariant repair (#405) ---
        from swarm.server.invariants import InvariantReconciler

        self.invariants = InvariantReconciler(
            task_board=self.task_board,
            task_history=self.task_history,
            drone_log=self.drone_log,
            blocker_store=self.blocker_store,
            get_workers=lambda: self.workers,
        )
        # --- PlaybookOps: recall, synthesis, outcome attribution ---
        from swarm.server.playbook_ops import PlaybookOps

        self.playbook_ops = PlaybookOps(
            get_store=lambda: self.playbook_store,
            get_synthesizer=lambda: self.playbook_synthesizer,
            get_config=lambda: self.config.playbooks,
            drone_log=self.drone_log,
            task_board=self.task_board,
            track_task=self._track_task,
            get_worker=self.get_worker,
        )
        # --- TaskCoordinator: assign / start / complete / handoff lifecycle ---
        from swarm.server.task_coordinator import TaskCoordinator

        self.tasks_coord = TaskCoordinator(self)
        self._wire_task_board()
        self._wire_pipeline_engine()

    # --- Backward-compat delegation properties (BackgroundLoopRunner) ---
    #
    # tests/test_daemon.py historically reached into ``daemon._heartbeat_task``
    # / ``_usage_task`` / ``_mtime_task`` to drive _cancel_timers and to
    # assert lifecycle. These shims keep those tests working without
    # forcing a parallel rename pass.  Writes update the runner's
    # internal registry so a None assignment is honoured (the test for
    # ``_mtime_task = None`` relies on it).

    @property
    def _heartbeat_task(self) -> asyncio.Task[None] | None:
        return self.loop_runner.get("heartbeat")

    @_heartbeat_task.setter
    def _heartbeat_task(self, value: asyncio.Task[None] | None) -> None:
        self._set_loop_task("heartbeat", value)

    @property
    def _usage_task(self) -> asyncio.Task[None] | None:
        return self.loop_runner.get("usage")

    @_usage_task.setter
    def _usage_task(self, value: asyncio.Task[None] | None) -> None:
        self._set_loop_task("usage", value)

    @property
    def _mtime_task(self) -> asyncio.Task[None] | None:
        return self.loop_runner.get("mtime")

    @_mtime_task.setter
    def _mtime_task(self, value: asyncio.Task[None] | None) -> None:
        self._set_loop_task("mtime", value)

    def _set_loop_task(self, name: str, value: asyncio.Task[None] | None) -> None:
        """Test-only setter: forces a specific task object into the runner.

        Production code calls ``loop_runner.register`` + ``start_all``; this
        path exists so tests that hand-build a task can still wire it in.
        Auto-initialises ``loop_runner`` because many test fixtures
        bypass ``__init__`` via ``SwarmDaemon.__new__`` and then set
        ``_mtime_task`` / ``_usage_task`` / ``_heartbeat_task`` directly.
        """
        if not hasattr(self, "loop_runner") or self.loop_runner is None:
            self.loop_runner = BackgroundLoopRunner()
        if value is None:
            self.loop_runner._tasks.pop(name, None)
        else:
            self.loop_runner._tasks[name] = value

    # --- Backward-compat delegation properties (StatePublisher) ---

    @property
    def _state_dirty(self) -> bool:
        return self.publisher._state_dirty

    @_state_dirty.setter
    def _state_dirty(self, value: bool) -> None:
        self.publisher._state_dirty = value

    @property
    def _state_debounce_handle(self) -> asyncio.TimerHandle | None:
        return self.publisher._state_debounce_handle

    @_state_debounce_handle.setter
    def _state_debounce_handle(self, value: asyncio.TimerHandle | None) -> None:
        self.publisher._state_debounce_handle = value

    @property
    def _state_debounce_delay(self) -> float:
        return self.publisher._state_debounce_delay

    @_state_debounce_delay.setter
    def _state_debounce_delay(self, value: float) -> None:
        self.publisher._state_debounce_delay = value

    def _wire_task_board(self) -> None:
        """Wire task_board.on_change to auto-broadcast to WS clients."""
        self.task_board.on_change(self._on_task_board_changed)

    def _on_task_board_changed(self) -> None:
        self.publisher.on_task_board_changed()

    def _wire_pipeline_engine(self) -> None:
        """Wire pipeline engine change events to WS broadcasts + notifications."""
        self.pipeline_engine.on("change", self._on_pipeline_change)
        self.pipeline_engine.on("pipeline_started", self._on_pipeline_started)
        self.pipeline_engine.on("pipeline_finished", self._on_pipeline_finished)

    def _on_pipeline_change(self) -> None:
        self.publisher.on_pipeline_change()

    def _on_pipeline_started(self, pipeline: object) -> None:
        if self.notification_bus:
            self.notification_bus.emit_pipeline_started(getattr(pipeline, "name", "?"))

    def _on_pipeline_finished(self, pipeline: object, failed: bool) -> None:
        if self.notification_bus:
            self.notification_bus.emit_pipeline_finished(
                getattr(pipeline, "name", "?"), failed=failed
            )

    def _build_notification_bus(self, config: HiveConfig) -> NotificationBus:
        from swarm.notify.bus import filtered_backend

        bus = NotificationBus(debounce_seconds=config.notifications.debounce_seconds)
        if config.notifications.templates:
            bus.set_templates(config.notifications.templates)
        if config.notifications.terminal_bell:
            bus.add_backend(
                filtered_backend(terminal_bell_backend, config.notifications.terminal_events)
            )
        if config.notifications.desktop:
            bus.add_backend(filtered_backend(desktop_backend, config.notifications.desktop_events))
        if config.notifications.webhook.url:
            from swarm.notify.webhook import make_webhook_backend

            bus.add_backend(make_webhook_backend(config.notifications.webhook))
        if config.notifications.email.enabled:
            from swarm.notify.email import make_email_backend

            bus.add_backend(make_email_backend(config.notifications.email))
        return bus

    @staticmethod
    def _build_graph_manager(config: HiveConfig) -> GraphManager | None:
        """Build a GraphTokenManager if Graph client_id is configured."""
        if not config.graph_client_id:
            return None
        from swarm.auth.graph import GraphTokenManager

        return GraphTokenManager(
            config.graph_client_id,
            config.graph_tenant_id,
            port=config.port,
            domain=config.domain,
            client_secret=config.graph_client_secret,
        )

    def _rebuild_graph(self) -> None:
        """Rebuild graph manager and update email service reference."""
        self.graph_mgr = self._build_graph_manager(self.config)
        self.email._graph_mgr = self.graph_mgr

    @staticmethod
    def _build_jira_token_manager(config: HiveConfig) -> JiraTokenManager | None:
        """Build a JiraTokenManager if Jira OAuth is configured.

        Falls back to credentials stored in the token file when the YAML
        config doesn't contain ``client_secret`` (it's never serialized
        to YAML for security).
        """
        from swarm.auth.jira import JiraTokenManager

        j = config.jira
        client_id = j.client_id
        client_secret = j.resolved_client_secret()

        # Recover credentials from the token file when config is incomplete
        if not client_id or not client_secret:
            stored_id, stored_secret = JiraTokenManager.stored_credentials()
            client_id = client_id or stored_id
            client_secret = client_secret or stored_secret

        if not client_id or not client_secret:
            _log.info(
                "Jira token manager not built: client_id=%s client_secret=%s",
                bool(client_id),
                bool(client_secret),
            )
            return None

        mgr = JiraTokenManager(client_id, client_secret, port=config.port, domain=config.domain)
        _log.info("Jira token manager built: connected=%s", mgr.is_connected())
        return mgr

    def _rebuild_jira(self) -> None:
        """Rebuild Jira token manager and sync service after config change."""
        from swarm.integrations.jira import JiraSyncService

        old_mgr = self.jira_mgr
        new_mgr = self._build_jira_token_manager(self.config)

        # Preserve the existing connected manager if credentials match —
        # avoids losing Atlassian OAuth state during dev reloads.
        if (
            old_mgr is not None
            and old_mgr.is_connected()
            and new_mgr is not None
            and old_mgr.client_id == new_mgr.client_id
        ):
            _log.info("Jira rebuild: reusing existing connected token manager")
            new_mgr = old_mgr

        self.jira_mgr = new_mgr
        self.jira = JiraSyncService(self.config.jira, token_manager=self.jira_mgr)

    def _get_worker_state(self, name: str) -> str | None:
        """Return a worker's current state value, or None if not found."""
        w = self.get_worker(name)
        return w.state.value if w else None

    def _on_queen_queue_status_change(self, status: dict[str, Any]) -> None:
        """Broadcast queen queue status changes to WS clients."""
        self.broadcast_ws({"type": "queen_queue", **status})

    def _worker_descriptions(self) -> dict[str, str]:
        """Build a name→description map from config workers."""
        return {w.name: w.description for w in self.config.workers if w.description}

    def init_pilot(self, *, enabled: bool = True) -> DronePilot:
        """Create, wire, and start the drone pilot. Returns the pilot instance."""
        from swarm.queen.context import build_hive_context

        self.pilot = DronePilot(
            self.workers,
            self.drone_log,
            self.config.watch_interval,
            pool=self.pool,
            drone_config=self.config.drones,
            task_board=self.task_board,
            queen=self.queen,
            worker_descriptions=self._worker_descriptions(),
            context_builder=build_hive_context,
        )
        # Provide per-worker configs for worker-scoped approval rules & identity
        self.pilot._worker_configs = {wc.name: wc for wc in self.config.workers}
        self.pilot._decision_exec._worker_configs = self.pilot._worker_configs
        self.pilot.on_escalate(self._on_escalation)
        self.pilot.on_workers_changed(self._on_workers_changed)
        self.pilot.on_task_assigned(self._on_task_assigned)
        self.pilot.on_state_changed(self._on_state_changed)
        self.pilot.on_proposal(self.queue_proposal)
        self.pilot.on_task_done(self._on_task_done)
        self.pilot.set_pending_proposals_check(self.proposal_store.has_pending)
        self.pilot.set_pending_proposals_for_worker(
            lambda name: bool(self.proposal_store.pending_for_worker(name))
        )
        # Task #225 Phase 2: wire the idle-watcher's PTY send to the real
        # daemon.send_to_worker now that it exists. Before this call the
        # watcher was instantiated with a no-op sender; unwiring post-init
        # would otherwise be harder to test.
        from swarm.mcp.server import get_worker_last_mcp_activity

        # Task #546: when a watcher gives up nudging a worker after
        # idle_nudge_max_repeats no-progress repeats, it surfaces one
        # operator-facing notification instead of looping forever.
        def _escalate_idle_to_operator(worker_name: str, detail: str) -> None:
            self.push_notification(
                event="idle_nudge_escalated",
                worker=worker_name,
                message=f"Worker stuck/awaiting input: {detail}",
                priority="high",
            )

        self.pilot.set_idle_nudge_sender(
            self.send_to_worker,
            message_store=getattr(self, "message_store", None),
            blocker_store=getattr(self, "blocker_store", None),
            mcp_activity_lookup=get_worker_last_mcp_activity,
            daemon_start_time=getattr(self, "daemon_start_time", None),
            interrupt_worker=self.interrupt_worker,
            spawn_handoff_task=self._spawn_handoff_task,
            escalate_to_operator=_escalate_idle_to_operator,
        )
        # Wire the Dreamer drone's read sources. The drone_log already
        # holds a reference to the BuzzStore (used for buzz log writes);
        # we pull it back out for the dreamer's read path so both the
        # writer and the miner share one store.
        self.pilot.set_dreamer_stores(
            buzz_store=getattr(self.drone_log, "_buzz_store", None),
            learnings_store=getattr(self, "queen_chat", None),
        )
        self.drone_log.on_entry(self._on_drone_entry)

        self.tasks._pilot = self.pilot

        # Wire oversight monitor
        from swarm.queen.oversight import OversightMonitor

        self._oversight_monitor = OversightMonitor(self.config.queen.oversight)
        self.pilot.set_oversight(self._oversight_monitor)
        self.pilot.on("oversight_alert", self._on_oversight_alert)
        self.pilot.on("operator_terminal_approval", self._on_operator_terminal_approval)
        self.pilot.on("park_proposal", self._on_park_proposal)

        self.pilot.start()
        self.pilot.enabled = enabled
        _log.info("pilot initialized (enabled=%s)", enabled)
        return self.pilot

    def _init_test_mode(self) -> None:
        """Initialize test mode: TestRunLog, TestOperator, wire events."""
        self.test_runner.init_test_mode()

    def _load_test_tasks(self) -> None:
        """Load tasks from the test project's tasks.yaml into the task board."""
        self.test_runner.load_test_tasks()

    def _on_test_complete(self) -> None:
        """Called when hive completes in test mode — trigger report generation."""
        self.test_runner.on_test_complete()

    # --- InvariantReconciler shims (extracted to swarm.server.invariants) ---
    #
    # These four delegate to ``self.invariants`` so callers that still
    # reach in via ``daemon._working_workers()`` /
    # ``daemon._run_invariant_reconciliation(reason)`` (tests, future
    # additions) keep working without a parallel rename.

    def _reconcile_active_per_worker(self) -> None:
        """Demote stale concurrent ACTIVE tasks at boot (delegates)."""
        self.invariants.reconcile_active_per_worker()

    def _working_workers(self) -> set[str]:
        return self.invariants.working_workers()

    def _blocked_task_ids(self) -> set[str]:
        return self.invariants.blocked_task_ids()

    def _run_invariant_reconciliation(self, reason: str) -> None:
        self.invariants.run(reason)

    async def start(self) -> None:
        """Discover workers and start the pilot loop."""
        # Prune old log entries from the SQLite store on startup
        self.drone_log.prune_store()
        # Prune old (read) inter-worker messages too. The periodic
        # _db_maintenance_loop repeats this daily; the startup pass keeps a
        # long-down daemon from coming back to a bloated table.
        self._prune_messages()

        self._reconcile_active_per_worker()

        # Task #226: defensively broadcast ``tools/list_changed`` to any
        # MCP session that raced the daemon startup and subscribed before
        # tool registration completed. The primary path is "push on
        # connect" in the SSE handlers; this is belt-and-suspenders for
        # the rare case where a client gets a GET /mcp response before
        # module-level ``TOOLS`` is fully assembled. No-op when no
        # sessions are subscribed yet (the normal fresh-start case).
        try:
            from swarm.mcp.server import broadcast_tools_list_changed

            await broadcast_tools_list_changed()
        except Exception:
            _log.debug("startup broadcast_tools_list_changed failed", exc_info=True)

        await self.discover()

        # Reconcile the Queen's on-disk CLAUDE.md against the shipped
        # QUEEN_SYSTEM_PROMPT constant before (re)starting her PTY.
        # Runs on every daemon boot so existing-Queen reloads also pick
        # up new shipped content.  See task #254 for the design.
        try:
            from swarm.queen.runtime import QUEEN_WORK_DIR, reconcile_queen_claude_md

            _reconcile_result = reconcile_queen_claude_md(QUEEN_WORK_DIR)
            self._handle_queen_claude_md_reconcile(_reconcile_result)
        except Exception:
            _log.warning("queen CLAUDE.md reconcile failed — continuing", exc_info=True)

        # Spawn the Queen if configured and not already running.  Must
        # happen after discover() so we don't double-spawn across daemon
        # reloads (her PTY survives os.execv).
        if self.pool is not None:
            try:
                from swarm.queen.runtime import ensure_queen_running

                await ensure_queen_running(self.pool, self.workers, self.config)
            except Exception:
                _log.warning("queen startup failed — continuing without her", exc_info=True)

        # Write per-worker .mcp.json so each worker's MCP calls include identity
        self._write_worker_mcp_configs()

        # Install Swarm slash commands into each worker's .claude/commands/
        # and Skills into each worker's .claude/skills/.
        self._install_worker_artifacts()

        if not self.workers:
            # No live processes yet — this is expected on a fresh
            # ``swarm start`` before any ``swarm launch``.  Log the
            # configured count at INFO so it's clear the daemon knows
            # about workers, they just aren't running.
            _log.info(
                "0 running workers (%d configured in %s)",
                len(self.config.workers),
                getattr(self.config, "config_source", "config"),
            )
        else:
            _log.info("found %d workers", len(self.workers))
            self.init_pilot(enabled=self.config.drones.enabled)
            _log.info(
                "daemon started — drone pilot %s",
                "active" if self.config.drones.enabled else "disabled",
            )

        # Background tasks start regardless of worker count.  All periodic
        # loops are registered with the BackgroundLoopRunner so the
        # cancellation list in stop() can't drift out of sync with the
        # start list here.
        #
        # Config mtime watcher needs its anchor primed before the loop
        # starts, and is only needed when using YAML (no swarm_db).
        mtime_enabled = not self.swarm_db.connected
        if mtime_enabled and self.config.source_path:
            sp = Path(self.config.source_path)
            if sp.exists():
                self.config_mgr._config_mtime = sp.stat().st_mtime

        self.loop_runner.register("heartbeat", self._heartbeat_loop)
        self.loop_runner.register("usage", self._usage_refresh_loop)
        self.loop_runner.register("conflict", self._conflict_check_loop)
        self.loop_runner.register("jira_sync", self.jira_svc.sync_loop, enabled=self.jira.enabled)
        self.loop_runner.register("update_check", self._check_for_updates)
        self.loop_runner.register("ws_janitor", self.hub.ws_janitor_loop)
        self.loop_runner.register("mtime", self._watch_config_mtime, enabled=mtime_enabled)
        self.loop_runner.register(
            "resource", self._resource_monitor_loop, enabled=self.config.resources.enabled
        )
        self.loop_runner.register("backup", self._backup_loop)
        self.loop_runner.register("pipeline_schedule", self._pipeline_schedule_loop)
        self.loop_runner.register("db_maintenance", self._db_maintenance_loop)
        self.loop_runner.register("health_sweep", self._health_sweep_loop)
        self.loop_runner.register("daily_digest", self._daily_digest_loop)
        self.loop_runner.register("playbook_consolidation", self._playbook_consolidation_loop)
        self.loop_runner.register("invariant_reconcile", self._invariant_reconcile_loop)
        self.loop_runner.start_all()

    async def _health_sweep_loop(self) -> None:
        """Disk-space + DB-integrity sweep with URGENT notifications."""
        from swarm.server.health import HealthSweep

        sweep = HealthSweep(db=self.swarm_db, notify=lambda: self.notification_bus)
        await sweep.sweep_loop()

    async def _daily_digest_loop(self) -> None:
        """Once a day, push a 24h activity summary through the notify bus.

        Opt-in by event selection: the ``daily_digest`` event type is OFF
        unless the operator enables it in the notification matrix, so this
        loop costs nothing for operators who don't want it.
        """
        _DIGEST_INTERVAL = 86_400.0
        try:
            while True:
                await asyncio.sleep(_DIGEST_INTERVAL)
                try:
                    from swarm.analysis.throughput import compute_throughput
                    from swarm.notify.digest import build_digest

                    summary = compute_throughput(self.task_board.all_tasks, window_days=1)
                    title, message = build_digest(summary)
                    if self.notification_bus:
                        self.notification_bus.emit_daily_digest(title, message)
                except Exception:
                    _log.warning("daily digest failed", exc_info=True)
        except asyncio.CancelledError:
            return

    async def _db_maintenance_loop(self) -> None:
        """Periodic WAL checkpoint (5 min) and daily backup with rotation."""
        _WAL_INTERVAL = 300  # 5 minutes
        _BACKUP_INTERVAL = 86400  # 24 hours
        _BACKUP_KEEP_DAYS = 7
        last_backup = time.time()
        backup_dir = Path.home() / ".swarm" / "backups"
        try:
            while True:
                await asyncio.sleep(_WAL_INTERVAL)
                try:
                    self.swarm_db.checkpoint()
                except Exception:
                    # Bloat risk if the WAL never checkpoints — surface at WARNING.
                    _log.warning("WAL checkpoint failed", exc_info=True)
                if time.time() - last_backup >= _BACKUP_INTERVAL:
                    try:
                        backup_dir.mkdir(parents=True, exist_ok=True)
                        from datetime import datetime

                        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                        dest = backup_dir / f"swarm_{stamp}.db"
                        self.swarm_db.backup(dest)
                        # Rotate: remove backups older than _BACKUP_KEEP_DAYS
                        cutoff = time.time() - _BACKUP_KEEP_DAYS * 86400
                        for f in sorted(backup_dir.glob("swarm_*.db")):
                            if f.stat().st_mtime < cutoff:
                                f.unlink(missing_ok=True)
                                _log.debug("removed old backup %s", f.name)
                        last_backup = time.time()
                    except Exception:
                        # Data-safety op — operators run WARNING-level logs
                        # and need to know backups are silently failing.
                        _log.warning("DB backup failed", exc_info=True)
                    self._purge_queen_threads()
                    self._prune_messages()
        except asyncio.CancelledError:
            return

    def _prune_messages(self) -> int:
        """Prune read inter-worker messages past the retention window.

        Read-only by default (unread = unconsumed coordination, never auto-
        deleted); ``0`` retention means keep forever. Returns count removed
        (0 on skip/error).
        """
        try:
            days = getattr(self.config.coordination, "message_retention_days", 30)
            if days and days > 0:
                return self.message_store.prune(days)
        except Exception:
            _log.warning("message prune failed", exc_info=True)
        return 0

    def _purge_queen_threads(self) -> int:
        """Purge resolved Queen chat threads past the retention window.

        Keeps the queen_threads/queen_messages tables from growing
        unbounded once the history tab makes old threads worth keeping
        for a while. Active threads are never purged; ``0`` retention
        means keep forever. Returns the number removed (0 on skip/error).
        """
        try:
            chat = getattr(self, "queen_chat", None)
            days = getattr(self.config.queen, "queen_thread_retention_days", 90)
            if chat is not None and days and days > 0:
                return chat.purge_old(retention_days=days)
        except Exception:
            _log.warning("queen thread purge failed", exc_info=True)
        return 0

    async def _invariant_reconcile_loop(self) -> None:
        """Periodic INV-1/2 reconcile, independent of worker state changes.

        The reactive trigger (``_on_state_changed``) only fires when a worker
        leaves a working state, so a >1-ACTIVE violation created while a worker
        stays BUZZING would otherwise persist until it idled or the daemon
        restarted (the platform #604/#605 case — both tasks stayed ACTIVE for
        ~1.5h while platform kept working). This low-frequency sweep closes that
        window. ``reconcile_invariants`` only writes when a violation actually
        exists, so the steady-state cost is one cheap in-memory scan. ``0``
        disables; floored at 15s so a misconfig can't busy-loop.
        """
        try:
            while True:
                interval = float(self.config.drones.reconcile_interval_seconds)
                await asyncio.sleep(max(15.0, interval) if interval > 0 else 90.0)
                if interval <= 0:
                    continue  # disabled — idle-poll the config for runtime enable
                try:
                    self._run_invariant_reconciliation("periodic")
                except asyncio.CancelledError:
                    raise
                except Exception:
                    _log.warning("periodic invariant reconcile failed", exc_info=True)
        except asyncio.CancelledError:
            return

    async def _playbook_consolidation_loop(self) -> None:
        """Low-frequency sweep that merges same-scope near-duplicate
        playbooks via the headless Queen. Interval from
        ``PlaybookConfig.consolidation_interval_seconds`` (floored at
        300s so a misconfig can't busy-loop). No-op while playbooks are
        disabled. Clean CancelledError shutdown like the other timers.
        """
        try:
            while True:
                cfg = self.config.playbooks
                interval = max(300.0, float(cfg.consolidation_interval_seconds))
                await asyncio.sleep(interval)
                if not cfg.enabled:
                    continue
                try:
                    await self.playbook_consolidator.consolidate_once()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    _log.warning("playbook consolidation sweep failed", exc_info=True)
        except asyncio.CancelledError:
            return

    def _on_escalation(self, worker: Worker, reason: str) -> None:
        self.escalation.on_escalation(worker, reason)

    def _on_task_done(self, worker: Worker, task: SwarmTask, resolution: str = "") -> None:
        """Handle a task that appears complete — create a proposal for user approval."""
        self.proposal_coord.on_task_done(worker, task, resolution)

    def _on_park_proposal(self, worker: Worker, task_id: str, reason: str = "") -> None:
        """Oversight detected an operator-blocked stall — raise a park
        proposal. Resolve the task; if it left the board / is no longer
        ACTIVE the stall self-resolved, so there's nothing to park."""
        task = self.task_board.get(task_id) if task_id else None
        if task is None or task.status != TaskStatus.ACTIVE:
            return
        self.proposal_coord.on_park_proposal(worker, task, reason)

    def _worker_task_map(self) -> dict[str, str]:
        """Return {worker_name: task_title} for all assigned/in-progress tasks."""
        result: dict[str, str] = {}
        for t in self.task_board.active_tasks:
            if t.assigned_worker:
                result[t.assigned_worker] = t.title
        return result

    def _record_completion_verdict(self, task_id: str, done: bool, confidence: float) -> None:
        """Relay Queen's completion verdict to the drone task-lifecycle layer.

        The drone uses this to extend the re-propose cooldown when Queen is
        confidently sure the worker hasn't finished.  No-op if the pilot
        isn't running (pre-start, shutdown).
        """
        if self.pilot is None:
            return
        try:
            self.pilot._task_lifecycle.record_completion_verdict(task_id, done, confidence)
        except AttributeError:
            # Pilot may be a test double without the method wired.
            pass

    def _handle_queen_claude_md_reconcile(self, result: object) -> None:
        """React to a ``reconcile_queen_claude_md`` outcome at daemon startup.

        Drift-flagged results trigger a Queen-inbox notification (via the
        message store's ``finding`` channel) and a ``SYSTEM`` buzz log
        entry so the dashboard shows the event.  Other actions log at
        info / debug only.  Called at most once per daemon boot.
        """
        from swarm.queen.runtime import ReconcileAction

        action = getattr(result, "action", None)
        details = getattr(result, "details", "")
        if action == ReconcileAction.DRIFT_FLAGGED:
            _log.warning("queen CLAUDE.md drift: %s", details)
            try:
                self.drone_log.add(
                    SystemAction.STATE_TRANSITION,
                    "queen",
                    f"CLAUDE.md drift: {details}",
                    category=LogCategory.SYSTEM,
                )
            except Exception:
                _log.debug("drone_log drift entry failed", exc_info=True)
            try:
                from swarm.messages.store import Message

                self.message_store.send(
                    Message(
                        sender="daemon",
                        recipient="queen",
                        msg_type="finding",
                        content=(
                            "CLAUDE.md drift detected: the shipped "
                            "QUEEN_SYSTEM_PROMPT has changed and your on-disk "
                            "file has local edits. Reference copies written "
                            "as CLAUDE.md.shipped-latest and "
                            "CLAUDE.md.shipped-last alongside your live "
                            "CLAUDE.md in ~/.swarm/queen/workdir/. Operator "
                            "can reconcile via `swarm queen sync-claude-md "
                            "--accept-shipped` (take the new ship) or "
                            "`--keep-local` (acknowledge drift; keep local). "
                            "If your local edits look promotable upstream, "
                            "run `swarm queen contribute-claude-md` for the "
                            "status diff, then `--emit-patch <file>` or "
                            "`--open-pr` to land them in the shipped constant "
                            "(task #258 contribution flow). "
                            "See task #254 spec for the full reconcile mechanism."
                        ),
                    )
                )
            except Exception:
                _log.debug("queen inbox drift notification failed", exc_info=True)
        elif action == ReconcileAction.AUTO_UPDATED:
            _log.info("queen CLAUDE.md auto-updated to new shipped version")
        elif action in (ReconcileAction.SEEDED, ReconcileAction.MARKER_SEEDED):
            _log.debug("queen CLAUDE.md reconcile: %s — %s", action, details)

    def _on_workers_changed(self) -> None:
        self.publisher.on_workers_changed()

    def _on_oversight_alert(
        self, worker: Worker, signal: OversightSignal, result: OversightResult
    ) -> None:
        """Handle critical oversight alert — notify human via dashboard."""
        self.escalation.on_oversight_alert(worker, signal, result)

    def _on_operator_terminal_approval(
        self,
        worker: Worker,
        summary: str,
        prompt_type: str,
        pattern: str,
        prompt_snippet: str = "",
    ) -> None:
        """Broadcast operator terminal approval so the dashboard can offer Approve Always."""
        self.escalation.on_operator_terminal_approval(
            worker, summary, prompt_type, pattern, prompt_snippet
        )

    def _on_task_assigned(self, worker: Worker, task: SwarmTask, message: str = "") -> None:
        self.proposal_coord.on_task_assigned(worker, task, message)

    async def _deliver_auto_assignment(self, worker: Worker, task: SwarmTask, message: str) -> None:
        """Deliver an auto-approved task assignment via the standard assign_task path."""
        await self.proposal_coord._deliver_auto_assignment(worker, task, message)

    def _on_state_changed(self, worker: Worker) -> None:
        self.publisher.on_state_changed(worker)
        # #405 INV-2: a worker leaving a working state must not keep an
        # ACTIVE task. Reconcile on every transition into a non-working
        # state (cheap — only repairs/persists when an invariant is
        # actually violated).
        if worker.state not in (WorkerState.BUZZING, WorkerState.WAITING):
            self._run_invariant_reconciliation(f"state→{worker.state.value}")

    def _mark_state_dirty(self) -> None:
        pub = self.publisher
        pub._state_dirty = True
        if pub._state_debounce_handle is not None:
            pub._state_debounce_handle.cancel()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._flush_state_broadcast()
            return
        pub._state_debounce_handle = loop.call_later(
            pub._state_debounce_delay, self._flush_state_broadcast
        )

    def _flush_state_broadcast(self) -> None:
        pub = self.publisher
        if not pub._state_dirty:
            return
        pub._state_dirty = False
        pub._state_debounce_handle = None
        self._broadcast_state()

    def _on_drone_entry(self, entry: SystemEntry) -> None:
        self.publisher.on_drone_entry(entry)

    def push_notification(
        self,
        *,
        event: str,
        worker: str,
        message: str,
        priority: str = "medium",
    ) -> None:
        """Push a notification to dashboard clients and store in history."""
        self.escalation.push_notification(
            event=event, worker=worker, message=message, priority=priority
        )

    async def _usage_refresh_loop(self) -> None:
        """Periodically read worker JSONL sessions to update token usage."""
        from swarm.worker.usage import (
            cache_read_ratio,
            estimate_context_usage,
            estimate_cost_for_provider,
            get_worker_usage,
        )

        sem = asyncio.Semaphore(_USAGE_CONCURRENCY)
        # Track last-emitted context pressure level per worker to avoid spam
        _ctx_levels: dict[str, str] = {}

        async def _refresh_one(worker: Worker) -> bool:
            async with sem:
                new_usage = await asyncio.to_thread(get_worker_usage, worker.path, self.start_time)
                new_usage.cost_usd = estimate_cost_for_provider(new_usage, worker.provider_name)
                changed = new_usage.total_tokens != worker.usage.total_tokens
                worker.usage = new_usage
                # Update context window estimate and cache efficiency
                worker.context_pct = estimate_context_usage(new_usage, worker.provider_name)
                worker.cache_ratio = cache_read_ratio(new_usage)
                return changed

        try:
            while True:
                await asyncio.sleep(_USAGE_REFRESH_INTERVAL)
                results = await asyncio.gather(
                    *(_refresh_one(w) for w in self.workers),
                    return_exceptions=True,
                )
                if any(r is True for r in results):
                    self._broadcast_usage()
                # Accumulate cost against assigned tasks
                self._accumulate_task_costs()
                # Expire stale file locks
                self._cleanup_file_locks()
                # Check context pressure thresholds
                dc = self.config.drones
                for w in self.workers:
                    self._check_context_pressure(
                        w,
                        dc.context_warning_threshold,
                        dc.context_critical_threshold,
                        _ctx_levels,
                    )
        except asyncio.CancelledError:
            return

    def _check_context_pressure(
        self,
        worker: Worker,
        warn_threshold: float,
        crit_threshold: float,
        levels: dict[str, str],
    ) -> None:
        """Emit context pressure notifications when thresholds are crossed."""
        pct = worker.context_pct
        if crit_threshold > 0 and pct >= crit_threshold:
            level = "critical"
        elif warn_threshold > 0 and pct >= warn_threshold:
            level = "warning"
        else:
            level = "normal"
        prev = levels.get(worker.name, "normal")
        if level != prev and level != "normal":
            self.notification_bus.emit_context_pressure(worker.name, pct, level)
        levels[worker.name] = level

    def _accumulate_task_costs(self) -> None:
        """Accumulate worker cost deltas against their assigned tasks."""
        if not self.task_board:
            return
        # Build worker→first-task index once instead of scanning all_tasks per worker.
        task_by_worker: dict[str, SwarmTask] = {}
        for task in self.task_board.all_tasks:
            if task.assigned_worker and task.cost_budget > 0:
                task_by_worker.setdefault(task.assigned_worker, task)
        for w in self.workers:
            prev_cost = self._prev_worker_costs.get(w.name, 0.0)
            current_cost = w.usage.cost_usd
            delta = current_cost - prev_cost
            self._prev_worker_costs[w.name] = current_cost
            if delta <= 0 or not w.process:
                continue
            task = task_by_worker.get(w.name)
            if task is None:
                continue
            task.cost_spent += delta
            ratio = task.cost_spent / task.cost_budget
            if ratio >= 1.0:
                self.drone_log.add(
                    SystemAction.QUEEN_BLOCKED,
                    w.name,
                    f"task #{task.number} over budget"
                    f" (${task.cost_spent:.2f}/${task.cost_budget:.2f})",
                    category=LogCategory.DRONE,
                )
            elif ratio >= 0.7 and not task._cost_warned:
                task._cost_warned = True
                self.drone_log.add(
                    SystemAction.QUEEN_BLOCKED,
                    w.name,
                    f"task #{task.number} at {ratio:.0%} of budget"
                    f" (${task.cost_spent:.2f}/${task.cost_budget:.2f})",
                    category=LogCategory.DRONE,
                )

    def _consolidate_learnings(self, task: SwarmTask) -> None:
        """Capture worker's recent output as task learnings (delegates)."""
        self.playbook_ops.consolidate_learnings(task)

    def _write_worker_mcp_configs(self) -> None:
        """Write per-worker .mcp.json files with worker identity in the URL.

        Each worker gets ``?worker=<name>`` in the MCP URL so the daemon
        can identify which worker is calling MCP tools.
        """
        import json as _json

        port = self.config.port
        for w in self.workers:
            worker_dir = Path(w.path)
            if not worker_dir.is_dir():
                continue
            mcp_path = worker_dir / ".mcp.json"
            mcp_config = {
                "mcpServers": {
                    "swarm": {
                        "type": "http",
                        "url": f"http://localhost:{port}/mcp?worker={w.name}",
                    }
                }
            }
            try:
                mcp_path.write_text(_json.dumps(mcp_config, indent=2) + "\n")
            except OSError:
                _log.debug("failed to write .mcp.json for %s", w.name)

    def _install_worker_artifacts(self) -> None:
        """Install Swarm slash commands and Skills into each worker's workdir.

        Slash commands land in ``<workdir>/.claude/commands/`` so ``/swarm-*``
        shows up in Claude Code's ``/help`` and transcripts read cleanly.
        Skills land in ``<workdir>/.claude/skills/`` so multi-step coordination
        behaviors (``/swarm-checkpoint``, ``/swarm-coordinate``) have a
        structured home.  Both are idempotent and overwrite on every daemon
        start so updates propagate via Reload.
        """
        from swarm.hooks.install import (
            install_worker_commands,
            install_worker_skills,
        )
        from swarm.providers import get_provider

        store = getattr(self, "playbook_store", None)
        for w in self.workers:
            worker_dir = Path(w.path)
            if not worker_dir.is_dir():
                continue
            try:
                install_worker_commands(worker_dir)
            except Exception:
                _log.debug("failed to install slash commands for %s", w.name, exc_info=True)
            try:
                install_worker_skills(worker_dir)
            except Exception:
                _log.debug("failed to install skills for %s", w.name, exc_info=True)
            # Phase 3: render ACTIVE in-scope playbooks as native Skills —
            # Claude workers only (.claude/skills/ is Claude-specific;
            # other providers reach playbooks via swarm_get_playbooks).
            if store is None:
                continue
            try:
                if get_provider(w.provider_name).supports_hooks:
                    from swarm.playbooks.installer import install_worker_playbooks

                    install_worker_playbooks(worker_dir, store, worker_name=w.name)
            except Exception:
                _log.debug("failed to install playbooks for %s", w.name, exc_info=True)

    def _cleanup_file_locks(self) -> None:
        """Remove expired file locks."""
        if not self.file_locks:
            return
        now = time.time()
        expired = [p for p, (_, ts) in self.file_locks.items() if now - ts >= self._file_lock_ttl]
        for p in expired:
            del self.file_locks[p]

    async def _ws_janitor_loop(self) -> None:
        """Periodically cull dead WebSocket clients."""
        await self.hub.ws_janitor_loop()

    async def _conflict_check_loop(self) -> None:
        """Periodically check for file conflicts between workers."""
        from swarm.git.conflicts import detect_conflicts

        try:
            while True:
                await asyncio.sleep(30)
                # Feed file ownership from all workers (not just worktree)
                await self._update_file_ownership()

                wt_map: dict[str, Path] = {}
                for w in self.workers:
                    if w.repo_path:
                        wt_map[w.name] = Path(w.path)
                if not wt_map:
                    if self._conflicts:
                        self._conflicts = []
                        self.broadcast_ws({"type": "conflicts_cleared"})
                    continue
                found = await detect_conflicts(wt_map)
                new_list: list[ConflictEntry] = [
                    {"file": c.file_path, "workers": c.workers} for c in found
                ]
                if new_list != self._conflicts:
                    self._conflicts = new_list
                    if new_list:
                        self.broadcast_ws(
                            {
                                "type": "conflict_detected",
                                "conflicts": new_list,
                            }
                        )
                    else:
                        self.broadcast_ws({"type": "conflicts_cleared"})
        except asyncio.CancelledError:
            return

    async def _update_file_ownership(self) -> None:
        """Update file ownership map from runtime git diff data."""
        from swarm.coordination.ownership import OwnershipMode
        from swarm.git.conflicts import get_changed_files

        if self.file_ownership.mode == OwnershipMode.OFF:
            return

        eligible = [(w.name, Path(w.path)) for w in self.workers if Path(w.path).is_dir()]
        if not eligible:
            return

        async def _get(name: str, path: Path) -> tuple[str, set[str]]:
            try:
                files = await get_changed_files(path)
                return (name, files)
            except Exception:
                _log.debug("git change check failed for %s", name, exc_info=True)
                return (name, set())

        results = await asyncio.gather(*(_get(n, p) for n, p in eligible))
        changed: dict[str, set[str]] = {name: files for name, files in results if files}

        if changed:
            overlaps = self.file_ownership.update_from_conflicts(changed)
            if overlaps:
                self.broadcast_ws(
                    {
                        "type": "ownership_overlap",
                        "overlaps": [
                            {
                                "file": o.file_path,
                                "owner": o.owner,
                                "intruder": o.intruder,
                            }
                            for o in overlaps
                        ],
                    }
                )

    def _broadcast_usage(self) -> None:
        self.publisher.broadcast_usage()

    async def _collect_worker_pids(self) -> set[int]:
        """Collect live worker PIDs from the pool."""
        return await self.resource_mon.collect_worker_pids()

    def _handle_resource_snapshot(self, snap: Any) -> None:
        """Process a resource snapshot: broadcast, check pressure, alert D-state."""
        self.resource_mon.handle_snapshot(snap)

    async def _resource_monitor_loop(self) -> None:
        """Periodically snapshot system resources and broadcast to WS clients."""
        await self.resource_mon.monitor_loop()

    def get_resource_snapshot(self) -> dict[str, object] | None:
        """Return the most recent resource snapshot dict, or None."""
        return self.resource_mon.snapshot

    async def _heartbeat_loop(self) -> None:
        """Periodically check if worker display_state changed and broadcast.

        Catches time-based transitions (e.g. RESTING→SLEEPING) that happen
        between poll cycles, ensuring WS clients stay in sync.

        Also acts as a watchdog: if the pilot's poll loop has died, restart it.
        First check runs after 2s (fast startup), then every 8s.
        """
        try:
            first = True
            while True:
                await asyncio.sleep(_HEARTBEAT_INITIAL_DELAY if first else _HEARTBEAT_INTERVAL)
                first = False

                # Watchdog: revive pilot loop if it died unexpectedly
                if self.pilot and self.pilot.needs_restart():
                    _log.warning("heartbeat: pilot loop was dead — restarting")
                    await self.pilot.restart_loop()

                snapshot = {w.name: w.display_state.value for w in self.workers}
                if snapshot != self._heartbeat_snapshot:
                    self._heartbeat_snapshot = snapshot
                    self._broadcast_state()
                    # Worker state changes that affect the Queen's health
                    # (BUZZING↔RESTING, STUNG → offline) should refresh
                    # the chat-panel health strip immediately.
                    self._broadcast_queen_health()
        except asyncio.CancelledError:
            return

    def _broadcast_queen_health(self) -> None:
        """Push a queen.health WebSocket event with the current snapshot.

        Consumed by the chat-panel health strip; safe to call on any
        loop that observes state change.  Silently swallows failures
        since this is best-effort telemetry.
        """
        try:
            from swarm.server.routes.queen import build_queen_health

            payload = build_queen_health(self)
            self.broadcast_ws({"type": "queen.health", **payload})
        except Exception:
            _log.debug("queen health broadcast failed", exc_info=True)

    async def _check_for_updates(self) -> None:
        """Background update check — runs once after a 5s startup delay."""
        if os.environ.get("SWARM_DEV"):
            _log.debug("dev mode — skipping update check")
            return
        try:
            await asyncio.sleep(_UPDATE_CHECK_DELAY)
            from swarm.update import check_for_update, sync_team_config, update_result_to_dict

            result = await check_for_update()
            self._update_result = result
            if result.available:
                self.broadcast_ws({"type": "update_available", **update_result_to_dict(result)})
            await sync_team_config()
        except asyncio.CancelledError:
            return
        except Exception:
            _log.debug("background update check failed", exc_info=True)

    def queue_proposal(self, proposal: AssignmentProposal) -> None:
        """Accept a new Queen proposal for user review."""
        self.proposal_coord.queue_proposal(proposal)

    def _expire_stale_proposals(self) -> None:
        self.proposal_coord.expire_stale()

    def proposal_dict(self, proposal: AssignmentProposal) -> dict[str, Any]:
        """Serialize a proposal to a dict for API/WebSocket responses."""
        return self.proposal_coord.proposal_dict(proposal)

    def _broadcast_proposals(self) -> None:
        self.proposal_coord.broadcast()

    def apply_config(self) -> None:
        """Apply current config to pilot, queen, and notification bus.

        Encapsulates internal attribute updates so external callers
        (e.g. ConfigManager) don't need to reach into daemon internals.
        """
        # Reconfigure log level so changes take effect without restart
        from swarm.logging import setup_logging

        setup_logging(
            level=self.config.log_level,
            log_file=self.config.log_file,
            stderr=True,
        )

        if self.pilot:
            self.pilot.drone_config = self.config.drones
            self.pilot.enabled = self.config.drones.enabled
            self.pilot.set_poll_intervals(
                self.config.drones.poll_interval,
                self.config.drones.max_idle_interval,
            )
            self.pilot.interval = self.config.drones.poll_interval
            self.pilot.worker_descriptions = self._worker_descriptions()
            # Refresh per-worker configs for approval rules & identity
            wc_map = {wc.name: wc for wc in self.config.workers}
            self.pilot._worker_configs = wc_map
            if hasattr(self.pilot, "_decision_exec"):
                self.pilot._decision_exec._worker_configs = wc_map

        self.queen.enabled = self.config.queen.enabled
        self.queen.cooldown = self.config.queen.cooldown
        self.queen.system_prompt = self.config.queen.system_prompt
        self.queen.min_confidence = self.config.queen.min_confidence
        self.queen.auto_assign_tasks = self.config.queen.auto_assign_tasks
        self.notification_bus = self._build_notification_bus(self.config)
        # Update ProposalManager's reference (it captures a direct value, not a lambda)
        if hasattr(self, "proposals"):
            self.proposals._notification_bus = self.notification_bus

    async def reload_config(self, new_config: HiveConfig) -> None:
        """Hot-reload configuration. Updates pilot, queen, and notifies WS clients."""
        await self.config_mgr.reload(new_config)

        # Start resource monitor if enabled but not yet running.  The
        # runner's start() is a no-op when the task is already live, so
        # this is safe to call on every reload.
        if self.config.resources.enabled:
            self.loop_runner.start("resource")

    async def _watch_config_mtime(self) -> None:
        """Poll config file mtime every 30s and notify WS clients if changed."""
        await self.config_mgr.watch_mtime()

    async def _pipeline_schedule_loop(self) -> None:
        """Check for pipeline steps that should auto-start based on schedule."""
        try:
            while True:
                await asyncio.sleep(60)
                engine = getattr(self, "pipeline_engine", None)
                if engine:
                    engine.check_scheduled_steps()
        except asyncio.CancelledError:
            return
        except Exception:
            _log.debug("pipeline schedule loop error", exc_info=True)

    async def _backup_loop(self) -> None:
        """Periodically backup task state to disk (every 30 minutes)."""
        _BACKUP_INTERVAL = 1800  # 30 minutes
        try:
            while True:
                await asyncio.sleep(_BACKUP_INTERVAL)
                store = getattr(self.task_board, "_store", None)
                if store and hasattr(store, "backup"):
                    store.backup()
        except asyncio.CancelledError:
            return
        except Exception:
            # Task-state backups silently failing = data-loss risk on crash.
            _log.warning("backup loop error", exc_info=True)

    def _on_tunnel_state_change(self, state: TunnelState, detail: str) -> None:
        self.publisher.on_tunnel_state_change(state, detail)
        # The WS broadcast only reaches an open dashboard — an operator who
        # relies on the tunnel for access loses exactly that when it errors,
        # so push through the notification backends too.
        if state == TunnelState.ERROR and self.notification_bus:
            from swarm.notify.bus import EventType, NotifyEvent, Severity

            self.notification_bus.emit(
                NotifyEvent(
                    event_type=EventType.TUNNEL_DOWN,
                    title="Cloudflare tunnel down",
                    message=f"Tunnel errored: {detail or 'unknown error'}",
                    severity=Severity.URGENT,
                )
            )

    _MAX_BG_TASKS = 100

    def _track_task(self, task: asyncio.Task[object]) -> None:
        """Register a fire-and-forget task for cancellation at shutdown."""
        if len(self._bg_tasks) >= self._MAX_BG_TASKS:
            # Prune completed tasks that haven't been discarded yet
            done = {t for t in self._bg_tasks if t.done()}
            self._bg_tasks -= done
            if len(self._bg_tasks) >= self._MAX_BG_TASKS:
                _log.warning("bg_tasks at limit (%d) — skipping", len(self._bg_tasks))
                return
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    def broadcast_ws(self, data: dict[str, Any]) -> None:
        """Send a message to all connected WebSocket clients."""
        self.hub.broadcast(data)

    def _flush_broadcast(self, msg_type: str) -> None:
        """Flush a debounced broadcast for *msg_type*."""
        self.hub._flush_broadcast(msg_type)

    def _send_ws_now(self, data: dict[str, Any]) -> None:
        """Immediately send *data* to all connected WebSocket clients."""
        self.hub._send_ws_now(data)

    @staticmethod
    async def _safe_ws_send(
        ws: web.WebSocketResponse, payload: str, dead: list[web.WebSocketResponse]
    ) -> None:
        """Send a WS message, catching exceptions and discarding dead clients."""
        await BroadcastHub._safe_ws_send(ws, payload, dead)

    def _broadcast_state(self) -> None:
        self.publisher.broadcast_state()

    async def _cancel_timers(self) -> None:
        """Cancel all background timer tasks and await their completion."""
        if self.pilot:
            self.pilot.stop()
        # The BackgroundLoopRunner owns the periodic-loop task lifecycle;
        # cancel_all() awaits each task with return_exceptions=True so
        # an already-failed worker can't abort the rest of shutdown.
        await self.loop_runner.cancel_all()
        if self.publisher._state_debounce_handle is not None:
            self.publisher._state_debounce_handle.cancel()
            self.publisher._state_debounce_handle = None
        # _bg_tasks holds ad-hoc one-shot tasks (e.g. completion replies,
        # playbook fires) that aren't part of the periodic-loop set.
        cancelled: list[asyncio.Task[object]] = []
        for task in list(self._bg_tasks):
            task.cancel()
            if isinstance(task, asyncio.Task):
                cancelled.append(task)
        self._bg_tasks.clear()
        if cancelled:
            await asyncio.gather(*cancelled, return_exceptions=True)

    @staticmethod
    async def _close_ws_set(clients: set[web.WebSocketResponse]) -> None:
        """Close all WebSocket connections in a set, ignoring errors."""
        await BroadcastHub.close_ws_set(clients)

    async def stop(self) -> None:
        # Generate test report if a test run was active and no report was written.
        # Must run before cancelling bg tasks so the subprocess can finish.
        await self._generate_test_report_if_pending()

        self.queen_queue.cancel_all()
        try:
            await asyncio.wait_for(self._cancel_timers(), timeout=10.0)
        except TimeoutError:
            _log.warning("shutdown: timed out waiting for background tasks")
        # Stop cloudflare tunnel if running
        if self.tunnel.is_running:
            await self.tunnel.stop()
        # Close all WebSocket connections so runner.cleanup() doesn't hang
        await self._close_ws_set(self.hub.ws_clients)
        await self._close_ws_set(self.hub.terminal_ws_clients)
        # Persist proposals so pending items survive restarts
        try:
            self.proposal_store.save()
        except Exception:
            _log.debug("failed to save proposals on stop", exc_info=True)
        # Close the drone log's SQLite store
        self.drone_log.close()
        # Flush and close the unified database
        if hasattr(self, "swarm_db") and self.swarm_db.connected:
            try:
                self.swarm_db.checkpoint()
                self.swarm_db.close()
            except Exception:
                _log.debug("failed to close swarm_db on stop", exc_info=True)
        # Disconnect from the PTY pool (without killing the holder sidecar)
        if getattr(self, "pool", None) is not None and self.pool.is_connected:
            try:
                await self.pool.disconnect()
            except Exception:
                _log.debug("failed to disconnect pool on stop", exc_info=True)
        # Release the daemon lock file descriptor
        lock_fd = getattr(self, "_lock_fd", None)
        if lock_fd is not None:
            try:
                os.close(lock_fd)
            except OSError:
                pass
            self._lock_fd = None
        _log.info("daemon stopped")

    async def _generate_test_report_if_pending(self) -> None:
        """Generate a test report on shutdown if one wasn't produced during the run."""
        await self.test_runner.generate_report_if_pending()

    # --- Lookup helper ---

    def get_worker(self, name: str) -> Worker | None:
        """Find a worker by name."""
        return self.worker_svc.get_worker(name)

    def _require_worker(self, name: str) -> Worker:
        """Get a worker by name or raise WorkerNotFoundError."""
        return self.worker_svc.require_worker(name)

    def _require_task(
        self, task_id: str, allowed_statuses: set[TaskStatus] | None = None
    ) -> SwarmTask:
        """Delegate to TaskManager."""
        return self.tasks.require_task(task_id, allowed_statuses)

    # --- Per-worker operations ---

    async def send_to_worker(
        self,
        name: str,
        message: str,
        *,
        enter: bool = True,
        _log_operator: bool = True,
    ) -> None:
        """Send text to a worker's process. Pass ``enter=False`` to type
        the message into the input buffer without submitting (used by
        the Web Share Target flow)."""
        await self.worker_svc.send_to_worker(
            name, message, enter=enter, _log_operator=_log_operator
        )

    async def continue_worker(self, name: str) -> None:
        """Send Enter to a worker's process."""
        await self.worker_svc.continue_worker(name)

    async def interrupt_worker(self, name: str) -> None:
        """Send Ctrl-C to a worker's process."""
        await self.worker_svc.interrupt_worker(name)

    async def escape_worker(self, name: str) -> None:
        """Send Escape to a worker's process."""
        await self.worker_svc.escape_worker(name)

    async def force_rest_worker(self, name: str) -> None:
        """Operator override: force a worker into RESTING state.

        Used when state detection is wrong (e.g. PTY shows the idle
        prompt but the daemon still thinks the worker is BUZZING).
        Sends Escape via the PTY (clears any interruptable prompt) and
        directly sets ``worker.state = RESTING`` on the in-memory
        Worker so drones / sidebar / state-tracker all observe the
        new state immediately. The next state-tracker tick will either
        confirm RESTING (PTY agrees) or re-detect the real state.
        """
        from swarm.worker.worker import WorkerState

        await self.worker_svc.escape_worker(name)
        worker = self.get_worker(name)
        if worker is not None:
            worker.state = WorkerState.RESTING
            worker.state_since = time.time()

    async def arrow_up_worker(self, name: str) -> None:
        """Send Up Arrow to a worker's process."""
        await self.worker_svc.arrow_up_worker(name)

    async def arrow_down_worker(self, name: str) -> None:
        """Send Down Arrow to a worker's process."""
        await self.worker_svc.arrow_down_worker(name)

    async def arrow_right_worker(self, name: str) -> None:
        """Send Right Arrow to a worker's process."""
        await self.worker_svc.arrow_right_worker(name)

    async def arrow_left_worker(self, name: str) -> None:
        """Send Left Arrow to a worker's process."""
        await self.worker_svc.arrow_left_worker(name)

    async def redraw_worker(self, name: str) -> None:
        """Send SIGWINCH to force TUI redraw."""
        await self.worker_svc.redraw_worker(name)

    async def capture_worker_output(self, name: str, lines: int = 80) -> str:
        """Read a worker's process output buffer."""
        return await self.worker_svc.capture_output(name, lines=lines)

    async def safe_capture_output(self, name: str, lines: int = 80) -> str:
        """Read process output, returning a fallback string on failure."""
        return await self.worker_svc.safe_capture_output(name, lines=lines)

    async def discover(self) -> list[Worker]:
        """Discover existing workers. Updates self.workers."""
        return await self.worker_svc.discover()

    async def poll_once(self) -> bool:
        """Run one pilot poll cycle. Returns True if any action was taken."""
        if not self.pilot:
            return False
        return await self.pilot.poll_once()

    # --- Operation methods ---

    async def launch_workers(self, worker_configs: list[WorkerConfig]) -> list[Worker]:
        """Launch workers. Extends self.workers and updates pilot."""
        return await self.worker_svc.launch(worker_configs)

    async def spawn_worker(self, worker_config: WorkerConfig) -> Worker:
        """Spawn a single worker into the running session."""
        return await self.worker_svc.spawn(worker_config)

    async def sleep_worker(self, name: str) -> None:
        """Force a RESTING worker into SLEEPING."""
        await self.worker_svc.sleep_worker(name)

    async def kill_worker(self, name: str) -> None:
        """Kill a worker: mark STUNG, unassign tasks, broadcast."""
        await self.worker_svc.kill(name)

    async def revive_worker(self, name: str) -> None:
        """Revive a STUNG worker."""
        await self.worker_svc.revive(name)

    async def kill_session(self, *, all_sessions: bool = False) -> None:
        """Kill all workers and clean up."""
        await self.worker_svc.kill_session(all_sessions=all_sessions)

    def create_task(
        self,
        title: str,
        description: str = "",
        priority: TaskPriority = TaskPriority.NORMAL,
        task_type: TaskType = TaskType.CHORE,
        tags: list[str] | None = None,
        depends_on: list[str] | None = None,
        attachments: list[str] | None = None,
        source_email_id: str = "",
        actor: str = "user",
    ) -> SwarmTask:
        """Delegate to TaskManager."""
        return self.tasks.create_task(
            title=title,
            description=description,
            priority=priority,
            task_type=task_type,
            tags=tags,
            depends_on=depends_on,
            attachments=attachments,
            source_email_id=source_email_id,
            actor=actor,
        )

    # --- TaskCoordinator shims (extracted to swarm.server.task_coordinator) ---
    #
    # The lifecycle methods (`assign_task`, `start_task`, `complete_task`,
    # `_spawn_handoff_task`, `_maybe_seed_goal`, `assign_and_start_task`,
    # `_auto_start_next_assigned`, `_auto_resolve_attention_for_task`,
    # `_check_ownership`, `_send_completion_reply`, `retry_draft_reply`)
    # moved to :class:`TaskCoordinator` (Phase 3 of
    # ``docs/specs/daemon-god-object-refactor.md``).  The daemon keeps
    # these proxy shims so routes / MCP / tests don't need to know the
    # methods moved.

    def _check_ownership(self, worker_name: str) -> None:
        self.tasks_coord.check_ownership(worker_name)

    async def assign_task(self, task_id: str, worker_name: str, actor: str = "user") -> bool:
        return await self.tasks_coord.assign_task(task_id, worker_name, actor=actor)

    async def start_task(
        self, task_id: str, actor: str = "user", message: str | None = None
    ) -> bool:
        return await self.tasks_coord.start_task(task_id, actor=actor, message=message)

    async def _maybe_seed_goal(
        self, task: SwarmTask, worker_name: str, worker_prov: object
    ) -> None:
        await self.tasks_coord._maybe_seed_goal(task, worker_name, worker_prov)

    async def _spawn_handoff_task(self, recipient: str, message: object) -> bool:
        return await self.tasks_coord.spawn_handoff_task(recipient, message)

    async def assign_and_start_task(
        self,
        task_id: str,
        worker_name: str,
        actor: str = "user",
        message: str | None = None,
    ) -> bool:
        return await self.tasks_coord.assign_and_start_task(
            task_id, worker_name, actor=actor, message=message
        )

    def complete_task(
        self,
        task_id: str,
        actor: str = "user",
        resolution: str = "",
        *,
        verify: bool = True,
        force: bool = False,
    ) -> bool:
        return self.tasks_coord.complete_task(
            task_id, actor=actor, resolution=resolution, verify=verify, force=force
        )

    # --- PlaybookOps shims (extracted to swarm.server.playbook_ops) ---

    def _fire_playbook_synthesis(self, task: SwarmTask, resolution: str) -> None:
        self.playbook_ops.fire_synthesis(task, resolution)

    def _recall_playbooks_for_task(self, task: SwarmTask, worker_name: str) -> str:
        return self.playbook_ops.recall_for_task(task, worker_name)

    async def _attribute_playbook_outcome(self, task: SwarmTask, status: object) -> None:
        await self.playbook_ops.attribute_outcome(task, status)

    def _log_verifier_skip(self, task: SwarmTask, *, actor: str) -> None:
        self.playbook_ops.log_verifier_skip(task, actor=actor)

    def _auto_resolve_attention_for_task(self, task_id: str) -> None:
        self.tasks_coord.auto_resolve_attention_for_task(task_id)

    def _auto_start_next_assigned(self, worker_name: str | None) -> None:
        self.tasks_coord.auto_start_next_assigned(worker_name)

    async def _send_completion_reply(
        self,
        message_id: str,
        task_title: str,
        task_type: str,
        resolution: str,
        task_id: str = "",
    ) -> None:
        await self.tasks_coord._send_completion_reply(
            message_id, task_title, task_type, resolution, task_id
        )

    async def retry_draft_reply(self, task_id: str) -> None:
        await self.tasks_coord.retry_draft_reply(task_id)

    def unassign_task(self, task_id: str, actor: str = "user") -> bool:
        """Delegate to TaskManager."""
        return self.tasks.unassign_task(task_id, actor)

    def reopen_task(self, task_id: str, actor: str = "user") -> bool:
        """Delegate to TaskManager."""
        result = self.tasks.reopen_task(task_id, actor)
        if result:
            self.jira_svc.fire_export(task_id, "unassigned")
        return result

    def fail_task(self, task_id: str, actor: str = "user") -> bool:
        """Delegate to TaskManager."""
        result = self.tasks.fail_task(task_id, actor)
        if result:
            if hasattr(self, "pipeline_engine"):
                self.pipeline_engine.on_task_failed(task_id)
            self.jira_svc.fire_export(task_id, "failed")
        return result

    def remove_task(self, task_id: str, actor: str = "user") -> bool:
        """Delegate to TaskManager."""
        return self.tasks.remove_task(task_id, actor)

    def create_cross_task(self, **kwargs: object) -> SwarmTask:
        """Delegate to TaskManager."""
        return self.tasks.create_cross_task(**kwargs)

    def approve_cross_task(self, task_id: str, actor: str = "user") -> bool:
        """Delegate to TaskManager."""
        return self.tasks.approve_cross_task(task_id, actor)

    def reject_cross_task(self, task_id: str, actor: str = "user") -> bool:
        """Delegate to TaskManager."""
        return self.tasks.reject_cross_task(task_id, actor)

    def edit_task(
        self,
        task_id: str,
        title: str | None = None,
        description: str | None = None,
        priority: TaskPriority | None = None,
        task_type: TaskType | None = None,
        tags: list[str] | None = None,
        attachments: list[str] | None = None,
        depends_on: list[str] | None = None,
        source_worker: str | None = None,
        target_worker: str | None = None,
        dependency_type: str | None = None,
        acceptance_criteria: list[str] | None = None,
        context_refs: list[str] | None = None,
        actor: str = "user",
    ) -> bool:
        """Delegate to TaskManager."""
        return self.tasks.edit_task(
            task_id,
            title=title,
            description=description,
            priority=priority,
            task_type=task_type,
            tags=tags,
            attachments=attachments,
            depends_on=depends_on,
            source_worker=source_worker,
            target_worker=target_worker,
            dependency_type=dependency_type,
            acceptance_criteria=acceptance_criteria,
            context_refs=context_refs,
            actor=actor,
        )

    async def approve_proposal(self, proposal_id: str) -> bool:
        """Approve a Queen proposal — delegates to ProposalCoordinator."""
        return await self.proposal_coord.approve(proposal_id)

    def reject_proposal(self, proposal_id: str, reason: str = "") -> bool:
        """Reject a Queen proposal — delegates to ProposalCoordinator."""
        return self.proposal_coord.reject(proposal_id, reason=reason)

    def reject_all_proposals(self) -> int:
        """Reject all pending proposals — delegates to ProposalCoordinator."""
        return self.proposal_coord.reject_all()

    async def approve_all_proposals(self) -> int:
        """Approve all pending proposals. Returns count approved."""
        return await self.proposal_coord.approve_all()

    def save_attachment(self, filename: str, data: bytes) -> str:
        """Delegate to EmailService."""
        return self.email.save_attachment(filename, data)

    async def create_task_smart(
        self,
        title: str = "",
        description: str = "",
        priority: TaskPriority = TaskPriority.NORMAL,
        task_type: TaskType | None = None,
        tags: list[str] | None = None,
        depends_on: list[str] | None = None,
        attachments: list[str] | None = None,
        source_email_id: str = "",
        actor: str = "user",
    ) -> SwarmTask:
        """Delegate to TaskManager."""
        return await self.tasks.create_task_smart(
            title=title,
            description=description,
            priority=priority,
            task_type=task_type,
            tags=tags,
            depends_on=depends_on,
            attachments=attachments,
            source_email_id=source_email_id,
            actor=actor,
        )

    async def fetch_and_save_image(self, url: str) -> str:
        """Delegate to EmailService."""
        return await self.email.fetch_and_save_image(url)

    async def process_email_data(
        self,
        subject: str,
        body_content: str,
        body_type: str,
        attachment_dicts: list[dict[str, Any]],
        effective_id: str,
        *,
        graph_token: str = "",
    ) -> dict[str, Any]:
        """Delegate to EmailService."""
        return await self.email.process_email_data(
            subject,
            body_content,
            body_type,
            attachment_dicts,
            effective_id,
            graph_token=graph_token,
        )

    def toggle_drones(self) -> bool:
        """Toggle drone pilot and persist to config. Returns new enabled state."""
        return self.config_mgr.toggle_drones()

    def check_config_file(self) -> bool:
        """Check if config file changed on disk; reload if so. Returns True if reloaded."""
        return self.config_mgr.check_file()

    async def continue_all(self) -> int:
        """Send Enter to all RESTING/WAITING workers. Returns count of workers continued."""
        return await self.worker_svc.continue_all()

    async def send_all(self, message: str) -> int:
        """Send a message to all workers. Returns count sent."""
        return await self.worker_svc.send_all(message)

    async def send_group(self, group_name: str, message: str) -> int:
        """Send a message to all workers in a group. Returns count sent."""
        return await self.worker_svc.send_group(group_name, message)

    async def gather_hive_context(self) -> str:
        """Delegate to QueenAnalyzer."""
        return await self.analyzer.gather_context()

    async def analyze_worker(self, worker_name: str, *, force: bool = False) -> dict[str, Any]:
        """Delegate to QueenAnalyzer."""
        return await self.analyzer.analyze_worker(worker_name, force=force)

    # coordinate_hive removed (task #253 spec B).

    async def apply_config_update(self, body: dict[str, Any]) -> dict[str, Any]:
        """Apply a partial config update from the API.

        Returns the structured ApplyResult dict (Phase 7 of #328) so the
        HTTP route can include it alongside the serialized config in the
        response, letting the dashboard render per-field success/failure
        toasts.  Raises ``ValueError`` on invalid input.
        """
        return await self.config_mgr.apply_update(body)

    def save_config(self) -> None:
        """Save config to disk and update mtime to prevent self-triggered reload."""
        self.config_mgr.save()


# Backward-compat re-exports — entry-point code moved to
# :mod:`swarm.server.runner` (audit finding #1, refactor spec
# ``docs/specs/daemon-god-object-refactor.md``).  External callers
# historically imported these names from ``swarm.server.daemon``;
# the indirection keeps them working without a coordinated rename
# pass across cli.py, web routes, and tests.
from swarm.server.runner import (  # noqa: E402, F401
    _DAEMON_LOCK_PATH,
    _acquire_daemon_lock,
    _clear_pycache,
    _db_ground_truth_counts,
    _exec_restart,
    _maybe_patch_systemd_unit,
    _pid_alive,
    _print_banner,
    _print_test_banner,
    _reachable_addresses,
    _read_lock_pid,
    _strip_config_flag,
    _wire_test_console,
    console_log,
    run_daemon,
    run_test_daemon,
)
