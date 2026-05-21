"""SwarmDaemon — long-running backend service."""

from __future__ import annotations

import asyncio
import os
import sys
import time
from collections.abc import Callable
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
from swarm.drones.log import DroneAction, DroneLog, LogCategory, SystemAction, SystemEntry
from swarm.drones.pilot import DronePilot
from swarm.drones.rules import Decision
from swarm.events import EventEmitter
from swarm.logging import get_logger
from swarm.notify.bus import NotificationBus
from swarm.notify.desktop import desktop_backend
from swarm.notify.terminal import terminal_bell_backend
from swarm.pty.process import ProcessError
from swarm.queen.queen import Queen
from swarm.queen.queue import QueenCallQueue
from swarm.server.analyzer import QueenAnalyzer
from swarm.server.broadcast import BroadcastHub
from swarm.server.config_manager import ConfigManager
from swarm.server.email_service import EmailService
from swarm.server.jira_service import JiraService
from swarm.server.proposals import ProposalManager
from swarm.server.resource_monitor import ResourceMonitor
from swarm.server.task_manager import TaskManager
from swarm.server.task_utils import log_task_exception as _log_task_exception
from swarm.server.test_runner import TestRunner
from swarm.server.worker_service import WorkerService
from swarm.tasks.board import TaskBoard
from swarm.tasks.history import TaskAction, TaskHistory
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
_PLAYBOOK_RECALL_LIMIT = 3  # max playbooks injected into a task dispatch
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
        self._jira_sync_task: asyncio.Task | None = None
        self._jira_auth_pending: dict[str, str] = {}  # state → csrf token
        self.pilot: DronePilot | None = None
        # --- BroadcastHub: WebSocket client management and debounced broadcasts ---
        self.hub = BroadcastHub(track_task=self._track_task)
        self._heartbeat_task: asyncio.Task | None = None
        self._usage_task: asyncio.Task | None = None
        self._conflict_task: asyncio.Task | None = None
        self._conflicts: list[ConflictEntry] = []
        self._heartbeat_snapshot: dict[str, str] = {}
        # In-flight Queen analysis tracking lives on self.analyzer
        self.start_time = time.time()
        self._mtime_task: asyncio.Task | None = None
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
        # Resource monitoring
        self._resource_task: asyncio.Task | None = None
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
        # Update detection
        self._update_result: UpdateResult | None = None
        self._update_task: asyncio.Task | None = None
        # Daemon lock FD — set externally by run_server()
        self._lock_fd: int | None = None
        self._wire_task_board()
        self._wire_pipeline_engine()

    # --- Backward-compat delegation properties (BroadcastHub) ---

    @property
    def ws_clients(self) -> set[web.WebSocketResponse]:
        return self.hub.ws_clients

    @ws_clients.setter
    def ws_clients(self, value: set[web.WebSocketResponse]) -> None:
        self.hub.ws_clients = value

    @property
    def terminal_ws_clients(self) -> set[web.WebSocketResponse]:
        return self.hub.terminal_ws_clients

    @terminal_ws_clients.setter
    def terminal_ws_clients(self, value: set[web.WebSocketResponse]) -> None:
        self.hub.terminal_ws_clients = value

    @property
    def _broadcast_hook(self) -> Callable[[dict[str, Any]], None] | None:
        return self.hub._broadcast_hook

    @_broadcast_hook.setter
    def _broadcast_hook(self, value: Callable[[dict[str, Any]], None] | None) -> None:
        self.hub._broadcast_hook = value

    @property
    def _broadcast_pending(self) -> dict[str, asyncio.TimerHandle]:
        return self.hub._broadcast_pending

    @property
    def _broadcast_latest(self) -> dict[str, dict[str, Any]]:
        return self.hub._broadcast_latest

    # --- Backward-compat delegation properties (ResourceMonitor) ---

    @property
    def _resource_snapshot(self) -> dict[str, object] | None:
        return self.resource_mon._resource_snapshot

    @_resource_snapshot.setter
    def _resource_snapshot(self, value: dict[str, object] | None) -> None:
        self.resource_mon._resource_snapshot = value

    @property
    def _prev_pressure_level(self) -> str:
        return self.resource_mon._prev_pressure_level

    @_prev_pressure_level.setter
    def _prev_pressure_level(self, value: str) -> None:
        self.resource_mon._prev_pressure_level = value

    # --- Backward-compat delegation properties (EscalationHandler) ---

    @property
    def _notification_history(self) -> list[dict[str, Any]]:
        return self.escalation._notification_history

    @_notification_history.setter
    def _notification_history(self, value: list[dict[str, Any]]) -> None:
        self.escalation._notification_history = value

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
        """Wire pipeline engine change events to WS broadcasts."""
        self.pipeline_engine.on("change", self._on_pipeline_change)

    def _on_pipeline_change(self) -> None:
        self.publisher.on_pipeline_change()

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
        self.pilot.set_pending_proposals_check(lambda: bool(self.proposal_store.pending))
        self.pilot.set_pending_proposals_for_worker(
            lambda name: bool(self.proposal_store.pending_for_worker(name))
        )
        # Task #225 Phase 2: wire the idle-watcher's PTY send to the real
        # daemon.send_to_worker now that it exists. Before this call the
        # watcher was instantiated with a no-op sender; unwiring post-init
        # would otherwise be harder to test.
        from swarm.mcp.server import get_worker_last_mcp_activity

        self.pilot.set_idle_nudge_sender(
            self.send_to_worker,
            message_store=getattr(self, "message_store", None),
            blocker_store=getattr(self, "blocker_store", None),
            mcp_activity_lookup=get_worker_last_mcp_activity,
            daemon_start_time=getattr(self, "daemon_start_time", None),
            interrupt_worker=self.interrupt_worker,
            spawn_handoff_task=self._spawn_handoff_task,
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

    def _reconcile_active_per_worker(self) -> None:
        """Demote stale concurrent ACTIVE tasks at boot.

        Older daemon versions left prior ACTIVE tasks ACTIVE when a newer one
        was dispatched, so the board could accumulate multiple ACTIVE rows
        per worker. The dashboard's IN PROGRESS label must reflect what the
        worker is actually processing, so on boot we keep the most recently
        updated ACTIVE per worker and demote the rest to ASSIGNED.
        """
        # #405: full INV-1/2/3 + operator-action reconciliation (was a
        # startup-only >1-ACTIVE sweep). Repairs the live corrupt records
        # and buzz-logs each so the operator can audit auto-corrections.
        self._run_invariant_reconciliation("startup")

    def _working_workers(self) -> set[str]:
        """Workers genuinely engaged on a turn (BUZZING/WAITING). Anything
        else (RESTING/SLEEPING/STUNG) cannot legitimately hold an ACTIVE
        task (#405 INV-2)."""
        return {
            w.name for w in self.workers if w.state in (WorkerState.BUZZING, WorkerState.WAITING)
        }

    def _blocked_task_ids(self) -> set[str]:
        """IDs of ACTIVE/ASSIGNED tasks with a live ``swarm_report_blocker``
        binding — these park to BLOCKED (not ASSIGNED) under INV-2."""
        store = getattr(self, "blocker_store", None)
        if store is None or self.task_board is None:
            return set()
        bindings: set[tuple[str, int]] = set()
        for w in self.workers:
            try:
                for b in store.list_for_worker(w.name):
                    bindings.add((b.worker, b.task_number))
            except Exception:
                continue
        return {
            t.id
            for t in self.task_board.active_tasks()
            if (t.assigned_worker or "", t.number) in bindings
        }

    def _run_invariant_reconciliation(self, reason: str) -> None:
        """Run the task-board invariant reconciler with live worker/blocker
        state and buzz-log + history every auto-repair (#405)."""
        if self.task_board is None:
            return
        try:
            repairs = self.task_board.reconcile_invariants(
                working_workers=self._working_workers(),
                blocked_task_ids=self._blocked_task_ids(),
            )
        except Exception:
            _log.warning("invariant reconciliation failed", exc_info=True)
            return
        for r in repairs:
            detail = f"{reason}: #{r['task_id'][:8]} {r['from']}→{r['to']} ({r['reason']})"
            try:
                self.drone_log.add(
                    SystemAction.TASK_RECONCILED,
                    r.get("worker") or "system",
                    detail,
                    category=LogCategory.TASK,
                    metadata=dict(r),
                )
                self.task_history.append(
                    r["task_id"], TaskAction.UNASSIGNED, actor="system", detail=detail
                )
            except Exception:
                _log.debug("reconcile audit log failed", exc_info=True)
        if repairs:
            _log.info("invariant reconcile (%s): repaired %d records", reason, len(repairs))

    async def start(self) -> None:
        """Discover workers and start the pilot loop."""
        # Prune old log entries from the SQLite store on startup
        self.drone_log.prune_store()

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

        # Background tasks start regardless of worker count
        # Start heartbeat loop for display_state dirty-checking
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

        # Start periodic usage refresh (every 15s)
        self._usage_task = asyncio.create_task(self._usage_refresh_loop())

        # Start conflict detection loop (every 30s, only for worktree workers)
        self._conflict_task = asyncio.create_task(self._conflict_check_loop())

        # Start Jira sync loop (if enabled)
        if self.jira.enabled:
            self._jira_sync_task = asyncio.create_task(self._jira_sync_loop())

        # Start background update check (5s delay for WS clients to connect)
        self._update_task = asyncio.create_task(self._check_for_updates())

        # Start WebSocket janitor to cull stale clients periodically
        self._ws_janitor_task = asyncio.create_task(self.hub.ws_janitor_loop())

        # Start config file mtime watcher
        # Config mtime watcher — only needed when using YAML (no swarm_db)
        if not self.swarm_db.connected:
            if self.config.source_path:
                sp = Path(self.config.source_path)
                if sp.exists():
                    self.config_mgr._config_mtime = sp.stat().st_mtime
            self._mtime_task = asyncio.create_task(self._watch_config_mtime())
        else:
            self._mtime_task = None

        # Start resource monitor loop (if enabled)
        if self.config.resources.enabled:
            self._resource_task = asyncio.create_task(self._resource_monitor_loop())

        # Periodic task backup (every 30 minutes)
        self._backup_task = asyncio.create_task(self._backup_loop())

        # Pipeline schedule checker (every 60 seconds)
        self._pipeline_schedule_task = asyncio.create_task(self._pipeline_schedule_loop())

        # DB maintenance: WAL checkpoint every 5 min, daily backup
        self._db_maintenance_task = asyncio.create_task(self._db_maintenance_loop())

        # Playbook consolidation sweep (low-frequency; merges same-scope
        # near-duplicate playbooks via the headless Queen).
        self._playbook_consolidation_task = asyncio.create_task(self._playbook_consolidation_loop())

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
                        _log.debug("DB backup failed", exc_info=True)
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
            self.pilot.record_completion_verdict(task_id, done, confidence)
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

    def _init_verifier_drone(self) -> None:
        """Construct the verifier drone with daemon-scoped closures.

        Read-only surfaces:

        * ``diff_provider`` — best-effort ``git diff HEAD~1 -- .`` in
          the worker's repo path; empty string on any failure (tier 1
          treats empty as "no diff produced", which short-circuits to
          reopen — the right conservative behaviour).
        * ``check_evidence_provider`` — scans the in-memory drone log
          for ``/check`` markers from the worker.
        * ``peer_warnings_provider`` — pulls open warning messages on
          the task from the message store.

        Mutating surfaces:

        * ``send_warning`` — wraps ``message_store.send`` so verifier
          findings show up in the worker's normal inbox.
        * ``escalate_to_operator`` — opens a Queen thread (kind=
          ``verifier-escalation``) the operator sees in the dashboard.
          Stub-safe when no Queen is configured.
        """
        from swarm.drones.verifier import VerifierDrone
        from swarm.queen.verifier import VerifierClient

        self.verifier_drone = VerifierDrone(
            drone_log=self.drone_log,
            task_board=self.task_board,
            verifier_client=VerifierClient(),
            diff_provider=self._verifier_diff,
            check_evidence_provider=self._verifier_check_evidence,
            peer_warnings_provider=self._verifier_peer_warnings,
            send_warning=self._verifier_send_warning,
            escalate_to_operator=self._verifier_escalate,
            on_verdict=self._attribute_playbook_outcome,
        )

    async def _verifier_diff(self, task: SwarmTask) -> str:
        """Best-effort git diff for the verifier (tier 1 + tier 2 input)."""
        from swarm.drones.verifier import safe_git_diff

        worker = self.get_worker(task.assigned_worker or "")
        repo = getattr(worker, "path", None) if worker else None
        if not repo:
            return ""
        return await safe_git_diff(str(repo))

    def _verifier_check_evidence(self, worker_name: str) -> bool:
        """True when the worker has recent /check evidence in the buzz log."""
        from swarm.drones.verifier import has_check_evidence

        entries = list(getattr(self.drone_log, "entries", []))
        return has_check_evidence(entries, worker_name)

    def _verifier_peer_warnings(self, task_id: str) -> str:
        """Concatenate any unresolved peer warnings on this task."""
        store = getattr(self, "message_store", None)
        if store is None:
            return ""
        try:
            msgs = store.recent_for_task(task_id, msg_type="warning")
        except (AttributeError, TypeError):
            return ""
        return "\n".join(getattr(m, "content", "") for m in msgs)[:1000]

    async def _verifier_send_warning(
        self, *, to: str, msg_type: str, content: str, from_: str = "verifier"
    ) -> None:
        """Deliver verifier findings to the worker's inbox."""
        store = getattr(self, "message_store", None)
        if store is None:
            return
        try:
            store.send(from_, to, msg_type, content)
        except Exception:
            _log.warning("verifier send_warning failed", exc_info=True)

    async def _verifier_escalate(self, *, task: SwarmTask, reason: str, reopen_count: int) -> None:
        """Open a Queen thread when the self-loop guard trips."""
        queen_chat = getattr(self, "queen_chat", None)
        if queen_chat is None:
            return
        try:
            await queen_chat.open_thread(
                title=f"Verifier escalation: task #{task.number}",
                kind="verifier-escalation",
                worker_name=task.assigned_worker or "",
                task_id=task.id,
                seed_message=(
                    f"Verifier reopened task #{task.number} {reopen_count} times "
                    f"and the worker still hasn't passed verification.\n\n"
                    f"Latest reason: {reason}\n\n"
                    "This needs operator review."
                ),
            )
        except Exception:
            _log.warning("verifier escalation thread open failed", exc_info=True)

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
        self._state_dirty = True
        if self._state_debounce_handle is not None:
            self._state_debounce_handle.cancel()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._flush_state_broadcast()
            return
        self._state_debounce_handle = loop.call_later(
            self._state_debounce_delay, self._flush_state_broadcast
        )

    def _flush_state_broadcast(self) -> None:
        if not self._state_dirty:
            return
        self._state_dirty = False
        self._state_debounce_handle = None
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
        """Capture worker's recent output as task learnings."""
        if not task.assigned_worker:
            return
        worker = self.get_worker(task.assigned_worker)
        if not worker or not worker.process:
            return
        try:
            content = worker.process.get_content(30)
        except Exception:
            return
        if not content:
            return
        # Strip ANSI codes and take last meaningful lines
        import re

        clean = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", content)
        lines = [ln.strip() for ln in clean.strip().splitlines() if ln.strip()]
        if lines:
            task.learnings = "\n".join(lines[-15:])

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

    async def _jira_sync_loop(self) -> None:
        """Periodically import Jira issues into the task board."""
        await self.jira_svc.sync_loop()

    async def _run_jira_import(self) -> int:
        """Execute a single Jira import cycle. Returns count of new tasks."""
        return await self.jira_svc.run_import()

    async def jira_export_status(self, task_id: str, new_status: TaskStatus) -> bool:
        """Export a task status change to Jira."""
        return await self.jira_svc.export_status(task_id, new_status)

    def _fire_jira(self, task_id: str, action: str, coro_factory: Callable[..., Any]) -> None:
        """Schedule a Jira operation as fire-and-forget background task."""
        self.jira_svc.fire_jira(task_id, action, coro_factory)

    def _fire_jira_export(self, task_id: str, new_status: str) -> None:
        """Schedule Jira status export as fire-and-forget background task."""
        self.jira_svc.fire_export(task_id, new_status)

    def _fire_jira_assign(self, task_id: str) -> None:
        """Schedule Jira issue assignment as fire-and-forget background task."""
        self.jira_svc.fire_assign(task_id)

    def _fire_jira_completion(self, task_id: str) -> None:
        """Schedule Jira completion comment as fire-and-forget background task."""
        self.jira_svc.fire_completion(task_id)

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

        # Start resource monitor if enabled but not yet running
        rt = getattr(self, "_resource_task", None)
        if self.config.resources.enabled and (rt is None or rt.done()):
            self._resource_task = asyncio.create_task(self._resource_monitor_loop())

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
            _log.debug("backup loop error", exc_info=True)

    def _on_tunnel_state_change(self, state: TunnelState, detail: str) -> None:
        self.publisher.on_tunnel_state_change(state, detail)

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
        cancelled: list[asyncio.Task[object]] = []
        for t in (
            self._heartbeat_task,
            self._usage_task,
            self._mtime_task,
            getattr(self, "_conflict_task", None),
            getattr(self, "_update_task", None),
            getattr(self, "_resource_task", None),
            getattr(self, "_backup_task", None),
            getattr(self, "_pipeline_schedule_task", None),
            getattr(self, "_ws_janitor_task", None),
            getattr(self, "_db_maintenance_task", None),
            getattr(self, "_playbook_consolidation_task", None),
        ):
            if t:
                t.cancel()
                if isinstance(t, asyncio.Task):
                    cancelled.append(t)
        if self._state_debounce_handle is not None:
            self._state_debounce_handle.cancel()
            self._state_debounce_handle = None
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
        await self._close_ws_set(self.ws_clients)
        await self._close_ws_set(self.terminal_ws_clients)
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

    def _check_ownership(self, worker_name: str) -> None:
        """Check file ownership conflicts; raise in HARD_BLOCK, warn in WARNING mode."""
        from swarm.coordination.ownership import OwnershipMode

        ownership = getattr(self, "file_ownership", None)
        if ownership is None or ownership.mode == OwnershipMode.OFF:
            return
        worker_files = ownership.get_worker_files(worker_name)
        if not worker_files:
            return
        overlaps = ownership.check_overlap(worker_name, worker_files)
        if not overlaps:
            return
        overlap_str = ", ".join(f"{o.file_path} (owned by {o.owner})" for o in overlaps[:3])
        if ownership.mode == OwnershipMode.HARD_BLOCK:
            raise SwarmOperationError(f"File ownership conflict: {overlap_str}")
        _log.warning("ownership overlap for %s: %s", worker_name, overlap_str)
        self.drone_log.add(
            SystemAction.OVERSIGHT_SIGNAL,
            worker_name,
            f"ownership warning: {overlap_str}",
            category=LogCategory.WORKER,
        )

    async def assign_task(
        self,
        task_id: str,
        worker_name: str,
        actor: str = "user",
    ) -> bool:
        """Assign (queue) a task to a worker without sending it.

        The task moves to ASSIGNED status. Call :meth:`start_task` to
        actually send the task message to the worker's PTY.
        """
        self._require_worker(worker_name)
        self._check_ownership(worker_name)

        task = self.task_board.get(task_id)
        if not task:
            raise TaskOperationError(f"Task '{task_id}' not found")
        if not task.is_available:
            raise TaskOperationError(
                f"Task '{task_id}' is not available ({task.status.value})", status_code=409
            )

        result = self.task_board.assign(task_id, worker_name)
        if result:
            self.task_history.append(task_id, TaskAction.ASSIGNED, actor=actor, detail=worker_name)
            self.drone_log.add(
                SystemAction.TASK_ASSIGNED,
                worker_name,
                f"queued: {task.title}",
                category=LogCategory.TASK,
                metadata={"task_id": task.id},
            )
            if actor == "user":
                self.drone_log.add(
                    DroneAction.OPERATOR,
                    worker_name,
                    f"task queued: {task.title}",
                    category=LogCategory.OPERATOR,
                )
        return result

    async def start_task(
        self,
        task_id: str,
        actor: str = "user",
        message: str | None = None,
    ) -> bool:
        """Send an ASSIGNED task to the worker's PTY and start it.

        If *message* is provided (e.g. from a Queen proposal), it is
        appended as context to the auto-generated task message.
        """
        task = self.task_board.get(task_id)
        if not task:
            raise TaskOperationError(f"Task '{task_id}' not found")
        if task.status != TaskStatus.ASSIGNED:
            raise TaskOperationError(
                f"Task '{task_id}' must be ASSIGNED to start (is {task.status.value})",
                status_code=409,
            )
        worker_name = task.assigned_worker
        if not worker_name:
            raise TaskOperationError(f"Task '{task_id}' has no assigned worker")

        self._require_worker(worker_name)

        from swarm.providers import get_provider
        from swarm.server.messages import build_task_message

        worker_prov = get_provider(self._require_worker(worker_name).provider_name)
        msg = build_task_message(task, supports_slash_commands=worker_prov.supports_slash_commands)
        if message:
            msg = f"{msg}\n\nQueen context: {message}"

        pb_block = self._recall_playbooks_for_task(task, worker_name)
        if pb_block:
            msg = f"{msg}\n{pb_block}"

        _log.info(
            "starting task %s on %s (%d chars)",
            task_id[:8],
            worker_name,
            len(msg),
        )

        try:
            await self.send_to_worker(worker_name, msg, _log_operator=False)
            if "\n" in msg or len(msg) > 200:
                worker = self._require_worker(worker_name)
                await asyncio.sleep(0.3)
                proc = worker.process
                if proc and not proc.is_user_active:
                    await proc.send_enter()
        except (TimeoutError, ProcessError, OSError):
            _log.warning("failed to send task message to %s", worker_name, exc_info=True)
            self.task_board.unassign(task_id)
            self.task_history.append(
                task_id,
                TaskAction.UNASSIGNED,
                actor="system",
                detail=f"send failed to {worker_name} — returned to pending",
            )
            self.broadcast_ws(
                {"type": "task_send_failed", "worker": worker_name, "task_title": task.title}
            )
            self.drone_log.add(
                SystemAction.TASK_SEND_FAILED,
                worker_name,
                task.title,
                category=LogCategory.TASK,
                is_notification=True,
            )
            return False

        # Demote any other ACTIVE task for this worker — only one task per
        # worker can be IN PROGRESS at a time. Older dispatches still queued
        # in the PTY input buffer revert to ASSIGNED so the dashboard reflects
        # what the worker is actually processing right now.
        demoted = self.task_board.demote_other_active(worker_name, keep_task_id=task_id)
        for demoted_id in demoted:
            self.task_history.append(
                demoted_id,
                TaskAction.UNASSIGNED,
                actor="system",
                detail=f"demoted to ASSIGNED — {worker_name} started newer task",
            )
            self._fire_jira_export(demoted_id, "assigned")

        # Transition to IN_PROGRESS
        task.start()
        self.task_board._persist()
        self.task_board._notify()
        self.task_history.append(task_id, TaskAction.STARTED, actor=actor, detail=worker_name)
        self._fire_jira_export(task_id, "active")
        if self.pilot:
            self.pilot.wake_worker(worker_name)
        self.drone_log.add(
            DroneAction.OPERATOR if actor == "user" else DroneAction.AUTO_ASSIGNED,
            worker_name,
            f"task started: {task.title}",
            category=LogCategory.TASK,
        )

        await self._maybe_seed_goal(task, worker_name, worker_prov)
        return True

    async def _maybe_seed_goal(
        self, task: SwarmTask, worker_name: str, worker_prov: object
    ) -> None:
        """Seed a native ``/goal`` from the task's acceptance criteria.

        Hands the criteria to the provider's own ``/goal`` evaluator
        (Claude Code / Codex) so it runs the keep-working loop — Swarm
        builds no evaluator. Called only from :meth:`start_task` (the
        dispatch boundary), so it is naturally set-once-per-dispatch:
        idle-watcher nudges go through ``send_to_worker`` directly and
        never re-arm the goal. No-op unless the feature flag is on, the
        task has acceptance criteria, and the worker's provider has a
        native ``/goal``. Best-effort — the task message already shipped
        and the task is started, so a ``/goal`` send failure must not
        unwind that.
        """
        drones = self.config.drones
        if not (
            drones.native_goal_enabled
            and task.acceptance_criteria
            and getattr(worker_prov, "supports_native_goal", False)
        ):
            return
        try:
            from swarm.server.messages import render_goal_condition

            condition = render_goal_condition(
                task.acceptance_criteria, max_turns=drones.native_goal_max_turns
            )
            if not condition:
                return
            await self.send_to_worker(worker_name, f"/goal {condition}", _log_operator=False)
            await asyncio.sleep(0.3)
            proc = self._require_worker(worker_name).process
            if proc and not proc.is_user_active:
                await proc.send_enter()
            self.drone_log.add(
                SystemAction.GOAL_SET,
                worker_name,
                f"#{task.number} goal armed: {condition[:120]}",
                category=LogCategory.TASK,
                metadata={"task_id": task.id, "task_number": task.number},
            )
        except Exception:
            _log.warning(
                "native /goal seeding failed for #%s on %s",
                task.number,
                worker_name,
                exc_info=True,
            )

    async def _spawn_handoff_task(self, recipient: str, message: object) -> bool:
        """task #442: turn an actionable cross-worker handoff to an idle,
        task-less recipient into a *tracked* task assigned to that
        recipient — so the IdleWatcher carries it to completion instead
        of the handoff living only in a skip-prone one-shot nudge that a
        missed turn or a daemon restart silently loses (the #985 →
        realtruth incident; #441 was the manual backfill this removes
        the need for).

        Wired into ``InterWorkerMessageWatcher`` via
        ``set_idle_nudge_sender``. Returns True when a task was created
        and assigned. Idempotency is handled upstream: the watcher
        de-dupes per message id, and once this assignment lands the
        recipient has an active task so the watcher's ``has_task`` gate
        stops it re-firing.
        """
        board = getattr(self, "task_board", None)
        if board is None:
            return False
        sender = getattr(message, "sender", "") or "?"
        msg_type = getattr(message, "msg_type", "dependency")
        msg_id = getattr(message, "id", None)
        content = (getattr(message, "content", "") or "").strip()
        first_line = content.splitlines()[0] if content else "(no content)"
        title = f"Handoff from {sender}: {first_line[:70]}"
        description = (
            f"Auto-spawned by the inter-worker watcher (task #442): "
            f"{recipient} was idle and task-less when {sender} sent a "
            f"'{msg_type}' handoff (message #{msg_id}). Process the handoff "
            f"and complete this task.\n\n--- original message ---\n{content}"
        )
        try:
            task = board.create(
                title=title,
                description=description,
                tags=["auto-handoff"],
            )
        except Exception:
            _log.warning("spawn_handoff_task: create failed for %s", recipient, exc_info=True)
            return False
        try:
            return await self.assign_and_start_task(
                task.id, recipient, actor="drone:inter-worker-handoff"
            )
        except Exception:
            _log.warning(
                "spawn_handoff_task: assign_and_start failed for %s (task %s)",
                recipient,
                task.id,
                exc_info=True,
            )
            return False

    async def assign_and_start_task(
        self,
        task_id: str,
        worker_name: str,
        actor: str = "user",
        message: str | None = None,
    ) -> bool:
        """Assign and immediately start a task (used by drones/Queen)."""
        assigned = await self.assign_task(task_id, worker_name, actor=actor)
        if assigned:
            return await self.start_task(task_id, actor=actor, message=message)
        return False

    def complete_task(
        self,
        task_id: str,
        actor: str = "user",
        resolution: str = "",
        *,
        verify: bool = True,
    ) -> bool:
        """Complete a task. Raises if not found or wrong state.

        When the task originated from an email and Graph is configured,
        automatically drafts a reply via the Graph API.

        ``verify=True`` (default) fires the verifier drone asynchronously
        after a successful completion so tier-1 deterministic checks +
        tier-2 LLM judgment can either confirm or reopen the task. Pass
        ``verify=False`` from explicit operator/Queen overrides
        (``queen_force_complete_task``) — those are deliberate
        completions that the verifier must respect.
        """
        task = self._require_task(task_id, {TaskStatus.ASSIGNED, TaskStatus.ACTIVE})

        # Capture email info before completing (status changes on complete)
        source_email_id = task.source_email_id
        task_title = task.title
        task_type = task.task_type.value

        result = self.task_board.complete(task_id, resolution=resolution)
        if result:
            # Knowledge consolidation: capture worker's last output as learnings
            self._consolidate_learnings(task)
            # Signal pilot that a task was completed during this session
            # so hive_complete detection can distinguish fresh completions
            # from stale ones loaded from the persistent store.
            if self.pilot:
                self.pilot.mark_completion_seen()
            self.task_history.append(task_id, TaskAction.COMPLETED, actor=actor, detail=resolution)
            self.drone_log.add(
                SystemAction.TASK_COMPLETED,
                task.assigned_worker or actor,
                task_title,
                category=LogCategory.TASK,
            )
            self.push_notification(
                event="task_completed",
                worker=task.assigned_worker or actor,
                message=f"Task completed: {task_title}",
                priority="medium",
            )
            self.notification_bus.emit_task_completed(task.assigned_worker or actor, task_title)
            if hasattr(self, "pipeline_engine"):
                self.pipeline_engine.on_task_completed(task_id, resolution)
            self._fire_jira_assign(task_id)
            self._fire_jira_export(task_id, "done")
            self._fire_jira_completion(task_id)
            # Notify source worker for cross-project tasks
            if task.is_cross_project and task.source_worker:
                source = self.get_worker(task.source_worker)
                if source:
                    notify_msg = (
                        f"Cross-project task completed: {task_title}\n"
                        f"Resolution: {resolution or '(no resolution)'}"
                    )
                    try:
                        t = asyncio.create_task(
                            self.send_to_worker(task.source_worker, notify_msg, _log_operator=False)
                        )
                        t.add_done_callback(_log_task_exception)
                        self._track_task(t)
                    except RuntimeError:
                        pass  # No running event loop
            # Auto-draft reply for email-originated tasks (like Jira comments).
            # Use a distinct local name so we don't clobber the SwarmTask bound
            # at the top of this method — ``task.assigned_worker`` is read
            # again below for the post-ship self-loop (task #270 regression).
            if source_email_id and self.graph_mgr and resolution:
                try:
                    asyncio.get_running_loop()
                    reply_bg = asyncio.create_task(
                        self._send_completion_reply(
                            source_email_id, task_title, task_type, resolution, task_id
                        )
                    )
                    reply_bg.add_done_callback(_log_task_exception)
                    self._track_task(reply_bg)
                except RuntimeError:
                    pass  # No running event loop (test/CLI context)
            # Command Center: auto-resolve any active Attention threads
            # linked to this task. Threads with kind in queen-escalation /
            # escalation / proposal that carry the same ``task_id`` get
            # cleared so the operator's Attention queue doesn't accumulate
            # stale items after work ships.
            self._auto_resolve_attention_for_task(task_id)
            # Task #225 Phase 3: post-ship self-loop.  If the worker that just
            # shipped has another ASSIGNED task queued up, kick it off now so
            # the PTY keeps moving instead of parking at the idle prompt
            # waiting for a drone/Queen nudge.  We skip IN_PROGRESS follow-ups
            # (already mid-flight in some session) and all other states.
            self._auto_start_next_assigned(task.assigned_worker)
            # Item 4 of the 10-repo bundle: fire the verifier drone async.
            # Skipped on explicit operator overrides (verify=False) so
            # queen_force_complete_task isn't second-guessed.
            if verify:
                self._fire_verifier(task)
            else:
                self._log_verifier_skip(task, actor=actor)
            # Playbook synthesis (independent of verification): mine this
            # successful completion into reusable procedural memory.
            self._fire_playbook_synthesis(task, resolution)
        return result

    def _fire_playbook_synthesis(self, task: SwarmTask, resolution: str) -> None:
        """Schedule playbook synthesis for ``task`` as fire-and-forget.

        No-op without a running event loop (sync/CLI callers) or a wired
        synthesizer. ``PlaybookSynthesizer.synthesize`` never raises into
        the caller (it swallows everything but CancelledError), and the
        ``_log_task_exception`` callback catches anything stray — task
        completion must be unaffected by synthesis.
        """
        synth = getattr(self, "playbook_synthesizer", None)
        if synth is None:
            return
        worker = task.assigned_worker or ""
        try:
            t = asyncio.create_task(synth.synthesize(task, worker=worker, resolution=resolution))
            t.add_done_callback(_log_task_exception)
            self._track_task(t)
        except RuntimeError:
            # No running event loop (sync/CLI context).
            return

    def _recall_playbooks_for_task(self, task: SwarmTask, worker_name: str) -> str:
        """Phase 2 recall-at-dispatch: a delimited block of the most
        relevant ACTIVE in-scope playbooks for this task ('' if none /
        disabled / store absent). Marks each as applied + buzz-logs.
        Best-effort — never raises into the dispatch path.
        """
        store = getattr(self, "playbook_store", None)
        cfg = getattr(getattr(self, "config", None), "playbooks", None)
        if store is None or (cfg is not None and not cfg.enabled):
            return ""
        try:
            from swarm.playbooks.models import PlaybookStatus

            query = f"{task.title} {task.description or ''}".strip()
            if not query:
                return ""
            repo = getattr(task, "repo", "") or getattr(task, "project", "")
            allowed = {"global", f"worker:{worker_name}"}
            if repo:
                allowed.add(f"project:{repo}")
            hits = store.search(
                query,
                scope=None,
                status=PlaybookStatus.ACTIVE,
                limit=_PLAYBOOK_RECALL_LIMIT * 3,
            )
            chosen = [pb for pb in hits if pb.scope in allowed][:_PLAYBOOK_RECALL_LIMIT]
            if not chosen:
                return ""
            lines = [
                "",
                "--- Relevant playbooks (vetted from past successful work — "
                "apply if they fit, cite if used) ---",
            ]
            for pb in chosen:
                lines.append(f"\n[{pb.name}] {pb.title}\nWhen: {pb.trigger}\n{pb.body}")
                try:
                    store.mark_applied(pb.id, task_id=task.id, worker=worker_name)
                except Exception:
                    _log.debug("playbook mark_applied failed for %s", pb.name, exc_info=True)
            lines.append("--- end playbooks ---")
            if self.drone_log is not None:
                from swarm.drones.log import LogCategory, SystemAction

                self.drone_log.add(
                    SystemAction.PLAYBOOK_APPLIED,
                    worker_name,
                    f"#{task.number}: injected {len(chosen)} playbook(s)",
                    category=LogCategory.DRONE,
                )
            return "\n".join(lines)
        except Exception:
            _log.warning("playbook recall failed — dispatching without", exc_info=True)
            return ""

    async def _attribute_playbook_outcome(self, task: SwarmTask, status: object) -> None:
        """Phase 2 win/loss attribution, wired into the verifier's
        ``on_verdict`` hook. VERIFIED → win for every playbook applied to
        this task; REOPENED/ESCALATED → loss; SKIPPED/NOT_RUN → no signal.
        Then evaluate auto-promote / prune. Best-effort — never raises
        into the verification path.
        """
        store = getattr(self, "playbook_store", None)
        if store is None:
            return
        try:
            from swarm.tasks.task import VerificationStatus

            if status == VerificationStatus.VERIFIED:
                win = True
            elif status in (VerificationStatus.REOPENED, VerificationStatus.ESCALATED):
                win = False
            else:
                return  # SKIPPED / NOT_RUN — no outcome signal
            applied = store.playbooks_applied_to_task(task.id)
            if not applied:
                return
            cfg = self.config.playbooks
            for pid in applied:
                store.record_outcome(pid, win, task_id=task.id)
                pb = store.get_by_id(pid)
                if pb is None:
                    continue
                verdict = store.evaluate_lifecycle(
                    pb.name,
                    promote_uses=cfg.auto_promote_uses,
                    promote_winrate=cfg.auto_promote_winrate,
                    prune_uses=cfg.prune_min_uses,
                    prune_winrate=cfg.prune_max_winrate,
                )
                if verdict and self.drone_log is not None:
                    from swarm.drones.log import LogCategory, SystemAction

                    action = (
                        SystemAction.PLAYBOOK_PROMOTED
                        if verdict == "promoted"
                        else SystemAction.PLAYBOOK_RETIRED
                    )
                    self.drone_log.add(
                        action,
                        task.assigned_worker or "",
                        f"{pb.name}: {verdict} (winrate={pb.winrate:.0%}, uses={pb.uses})",
                        category=LogCategory.DRONE,
                    )
        except Exception:
            _log.warning("playbook outcome attribution failed", exc_info=True)

    def _fire_verifier(self, task: SwarmTask) -> None:
        """Schedule the verifier drone for ``task`` as fire-and-forget.

        No-op when there's no running event loop (sync/CLI callers) or
        when no verifier drone is wired (config disable / older
        deployments). The verifier itself never raises into the
        ``complete_task`` caller — :func:`fire_and_forget` swallows any
        exception so the task lifecycle is unaffected.
        """
        verifier = getattr(self, "verifier_drone", None)
        if verifier is None:
            return
        try:
            from swarm.drones.verifier import fire_and_forget

            t = asyncio.create_task(fire_and_forget(verifier, task))
            t.add_done_callback(_log_task_exception)
            self._track_task(t)
        except RuntimeError:
            # No running event loop (sync/CLI context); the verifier
            # only runs in the daemon's async lifecycle.
            return

    def _log_verifier_skip(self, task: SwarmTask, *, actor: str) -> None:
        """Log a force-complete skip under LogCategory.VERIFIER."""
        from swarm.drones.log import LogCategory, SystemAction
        from swarm.tasks.task import VerificationStatus

        task.verification_status = VerificationStatus.SKIPPED
        task.verification_reason = f"force-completed by {actor}"
        if self.task_board is not None:
            self.task_board.persist(task)
        if self.drone_log is not None:
            self.drone_log.add(
                SystemAction.VERIFIER_SKIPPED,
                task.assigned_worker or actor,
                f"#{task.number}: skipped — force-completed by {actor}",
                category=LogCategory.VERIFIER,
                metadata={"task_id": task.id, "task_number": task.number, "actor": actor},
            )

    def _auto_resolve_attention_for_task(self, task_id: str) -> None:
        """Resolve active Attention threads whose ``task_id`` matches.

        Best-effort: an exception here must never interrupt the
        completion path. Broadcasts a ``queen.thread`` resolved event so
        the dashboard clears the Attention card without polling.
        """
        chat = getattr(self, "queen_chat", None)
        if chat is None or not task_id:
            return
        try:
            active = chat.list_threads(status="active", limit=200)
        except Exception:
            return
        for thread in active:
            if thread.task_id != task_id:
                continue
            try:
                ok = chat.resolve_thread(
                    thread.id, resolved_by="queen", reason="upstream task DONE"
                )
            except Exception:
                continue
            if ok:
                try:
                    from swarm.server.routes.queen import _broadcast_thread

                    _broadcast_thread(self, thread.id, "resolved")
                except Exception:
                    pass

    def _auto_start_next_assigned(self, worker_name: str | None) -> None:
        """Fire-and-forget: start the next ASSIGNED task for *worker_name*.

        No-op when no such task exists, when there's no running event loop
        (sync/CLI callers), or when the worker name is empty. Intentionally
        picks the lowest task number so chained work ships in creation
        order rather than LIFO — matches operator expectations when a
        burst of related tasks gets filed.
        """
        if not worker_name or not self.task_board:
            return
        next_assigned = next(
            (
                t
                for t in sorted(
                    self.task_board.active_tasks_for_worker(worker_name),
                    key=lambda t: t.number,
                )
                if t.status == TaskStatus.ASSIGNED
            ),
            None,
        )
        if next_assigned is None:
            return
        try:
            t = asyncio.create_task(self.start_task(next_assigned.id, actor="auto-chain"))
            t.add_done_callback(_log_task_exception)
            self._track_task(t)
        except RuntimeError:
            # No running event loop (sync/CLI context) — leave the task
            # ASSIGNED; the idle-watcher or the next dashboard action
            # will pick it up.
            return

    async def _send_completion_reply(
        self,
        message_id: str,
        task_title: str,
        task_type: str,
        resolution: str,
        task_id: str = "",
    ) -> None:
        """Delegate to EmailService."""
        await self.email.send_completion_reply(
            message_id, task_title, task_type, resolution, task_id
        )

    async def retry_draft_reply(self, task_id: str) -> None:
        """Retry drafting an email reply for an already-completed task."""
        task = self._require_task(task_id)
        if not task.source_email_id:
            raise TaskOperationError("Task has no source email", status_code=409)
        if not task.resolution:
            raise TaskOperationError("Task has no resolution text", status_code=409)
        if not self.graph_mgr:
            raise TaskOperationError("Microsoft Graph not configured", status_code=409)

        await self.email.send_completion_reply(
            task.source_email_id, task.title, task.task_type.value, task.resolution, task_id
        )

    def unassign_task(self, task_id: str, actor: str = "user") -> bool:
        """Delegate to TaskManager."""
        return self.tasks.unassign_task(task_id, actor)

    def reopen_task(self, task_id: str, actor: str = "user") -> bool:
        """Delegate to TaskManager."""
        result = self.tasks.reopen_task(task_id, actor)
        if result:
            self._fire_jira_export(task_id, "unassigned")
        return result

    def fail_task(self, task_id: str, actor: str = "user") -> bool:
        """Delegate to TaskManager."""
        result = self.tasks.fail_task(task_id, actor)
        if result:
            if hasattr(self, "pipeline_engine"):
                self.pipeline_engine.on_task_failed(task_id)
            self._fire_jira_export(task_id, "failed")
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


_DAEMON_LOCK_PATH = Path.home() / ".swarm" / "daemon.lock"


def _read_lock_pid() -> int | None:
    """Read the PID from the daemon lock file, or None if unreadable."""
    try:
        text = _DAEMON_LOCK_PATH.read_text().strip()
        return int(text) if text else None
    except (OSError, ValueError):
        return None


def _pid_alive(pid: int) -> bool:
    """Check if a process is alive (signal 0 probe)."""
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _acquire_daemon_lock() -> int:
    """Acquire an exclusive lock on the daemon lock file.

    Uses ``fcntl.flock()`` which is automatically released when the
    process exits (even on crash).  Returns the open file descriptor
    so it stays alive for the process lifetime.

    If the lock is held by a dead process (e.g. orphaned child from
    SWARM_DEV execvp via ``uv run``), the stale lock is broken and
    re-acquired automatically.

    Raises ``SystemExit`` if another daemon already holds the lock
    and that process is still alive.
    """
    import fcntl

    _DAEMON_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(_DAEMON_LOCK_PATH), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        # Lock held — check if the holder is still alive
        holder_pid = _read_lock_pid()
        if holder_pid is not None and not _pid_alive(holder_pid):
            # Stale lock from a dead process — break it
            _log.warning("breaking stale daemon lock held by dead PID %d", holder_pid)
            os.close(fd)
            _DAEMON_LOCK_PATH.unlink(missing_ok=True)
            fd = os.open(str(_DAEMON_LOCK_PATH), os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                os.close(fd)
                raise SystemExit(
                    "Another swarm daemon is already running. Run 'swarm stop' to stop it."
                )
        else:
            os.close(fd)
            raise SystemExit(
                "Another swarm daemon is already running. Run 'swarm stop' to stop it."
            )
    # Write our PID for diagnostics
    os.ftruncate(fd, 0)
    os.lseek(fd, 0, os.SEEK_SET)
    os.write(fd, f"{os.getpid()}\n".encode())
    return fd


def _maybe_patch_systemd_unit() -> None:
    """Auto-patch existing systemd unit to use KillMode=process."""
    try:
        from swarm.service import ensure_killmode_process

        if ensure_killmode_process():
            _log.info("Patched systemd unit: KillMode=process (preserves workers across restarts)")
    except Exception:
        pass  # not critical — skip on non-systemd systems


async def run_daemon(
    config: HiveConfig, host: str = "localhost", port: int = 9090, *, test_mode: bool = False
) -> None:
    """Start the daemon with HTTP server."""
    import signal

    from swarm.server.api import create_app

    # Diagnostic: log cfg.workflows immediately on entry — if this is
    # already empty here, the wipe happened in cli.py between
    # ``_load_config_db_first`` and the call to ``run_daemon``.  If
    # it's correct here but ``SwarmDaemon.__init__`` later sees empty,
    # the wipe is in ``__init__`` itself.  WARNING level survives any
    # log-level filter (Amanda 2026-05-05).
    _log.warning(
        "run_daemon entry: config.workflows=%r config_source=%s argv=%r",
        config.workflows,
        getattr(config, "config_source", "<unset>"),
        sys.argv,
    )

    # Singleton lock — prevents two daemons from running simultaneously
    # and causing revive wars via the shared pty-holder.
    # The fd must stay open for the process lifetime; stored on the daemon.
    _daemon_lock_fd = _acquire_daemon_lock()

    _maybe_patch_systemd_unit()

    # Capture startup command for os.execv restart
    startup_argv = list(sys.argv)

    test_store = None
    if test_mode:
        test_store = FileTaskStore(path=Path.home() / ".swarm" / "test-tasks.json")
    daemon = SwarmDaemon(config, task_store=test_store)
    daemon._lock_fd = _daemon_lock_fd  # prevent GC / keep lock alive

    # Initialize the PTY process pool (starts holder sidecar if needed)
    from swarm.pty.pool import ProcessPool

    pool = ProcessPool()
    await pool.ensure_holder()
    daemon.pool = pool

    await daemon.start()

    # Initialize test mode components if enabled
    if test_mode:
        daemon._init_test_mode()

    app = create_app(daemon)

    # Graceful shutdown via signal — avoids KeyboardInterrupt race with aiohttp
    shutdown = asyncio.Event()
    app["shutdown_event"] = shutdown
    # Mutable container so the handler can set it without triggering
    # aiohttp's "changing state of started app" deprecation warning.
    app["restart_flag"] = {"requested": False}

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()

    _print_banner(daemon, host, port)

    # Wire runtime event logging to console
    if daemon.pilot:
        daemon.pilot.on_state_changed(
            lambda w: console_log(f'Worker "{w.name}" state -> {w.state.value}')
        )
        daemon.pilot.on_task_assigned(
            lambda w, t, m="": console_log(f'Task "{t.title}" assigned -> {w.name}')
        )
        daemon.pilot.on_workers_changed(lambda: console_log("Workers changed (add/remove)"))
        daemon.pilot.on_hive_empty(lambda: console_log("All workers gone", level="warn"))
        daemon.pilot.on_hive_complete(lambda: console_log("Hive complete — all tasks done"))

    daemon.task_board.on_change(lambda: console_log("Task board updated"))

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, shutdown.set)

    # If a restart was requested and the tunnel is running, auto-start it
    # after the new process comes up by checking for the marker file.
    if daemon.tunnel.consume_restart_marker():
        try:
            url = await daemon.tunnel.start()
            console_log(f"Tunnel auto-restarted: {url}")
        except Exception as exc:
            console_log(f"Tunnel auto-restart failed: {exc}", level="warn")

    await shutdown.wait()
    print("\nShutting down...", flush=True)

    # Save tunnel restart marker before stopping (only if restart requested)
    if app.get("restart_flag", {}).get("requested"):
        daemon.tunnel.save_restart_marker()

    await daemon.stop()
    try:
        await asyncio.wait_for(runner.cleanup(), timeout=5.0)
    except TimeoutError:
        _log.warning("shutdown: timed out waiting for HTTP runner cleanup")

    # If restart was requested (e.g. after update), replace process with new binary
    if app.get("restart_flag", {}).get("requested"):
        _exec_restart(daemon, startup_argv)


def _exec_restart(daemon: SwarmDaemon, startup_argv: list[str]) -> None:
    """Clear caches, release the daemon lock, and exec into a fresh process."""
    _clear_pycache()
    # Close DB connection before exec so the new process gets a clean connection
    if hasattr(daemon, "swarm_db") and daemon.swarm_db:
        try:
            daemon.swarm_db.checkpoint()
            daemon.swarm_db.close()
        except Exception:
            pass
    # Release daemon lock before exec so the new process image can acquire it
    lock_fd = getattr(daemon, "_lock_fd", None)
    if lock_fd is not None:
        try:
            os.close(lock_fd)
        except OSError:
            pass
    # Strip ``-c`` / ``--config`` from argv before exec.  Pre-fix a
    # legacy ``swarm.service`` ExecStart of
    # ``swarm serve -c ~/.config/swarm/config.yaml`` carried that
    # bypass through every reload.  The DB-first override at
    # ``_load_config_db_first`` now ignores it when the DB has data,
    # but once we know we're DB-canonical we should also stop
    # propagating the flag — otherwise the operator sees a
    # "ignoring --config X" WARNING on every restart even though
    # the value is moot.
    cleaned = _strip_config_flag(startup_argv)
    print("Restarting swarm...", flush=True)
    os.execv(cleaned[0], cleaned)


def _strip_config_flag(argv: list[str]) -> list[str]:
    """Return ``argv`` with any ``-c <path>`` / ``--config <path>`` removed.

    Handles all four forms: ``-c X``, ``-cX``, ``--config X``, ``--config=X``.
    """
    out: list[str] = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "-c" or a == "--config":
            i += 2  # skip flag and its value
            continue
        if a.startswith("-c") and len(a) > 2 and not a.startswith("--"):
            i += 1  # bundled ``-c<path>``
            continue
        if a.startswith("--config="):
            i += 1
            continue
        out.append(a)
        i += 1
    return out


def _clear_pycache() -> None:
    """Remove all __pycache__ dirs under the swarm source tree.

    Forces Python to recompile from .py source on the next import,
    guaranteeing that a restart picks up code changes.
    """
    import shutil

    import swarm

    src_root = Path(swarm.__file__).resolve().parent
    for cache_dir in src_root.rglob("__pycache__"):
        shutil.rmtree(cache_dir, ignore_errors=True)


def _reachable_addresses(host: str) -> list[str]:
    """Return a list of client-usable host addresses for the banner.

    ``0.0.0.0`` / ``::`` is a bind address ("listen on all interfaces"),
    NOT a client address.  Displaying it in the banner as
    ``http://0.0.0.0:9090`` is misleading and — on headless servers —
    actively harmful: operators logging in remotely copy the URL and
    then can't reach it, while modern Chrome (>=128, Private Network
    Access) explicitly blocks web origins loaded at 0.0.0.0 from
    opening WebSockets to themselves, which looks exactly like the
    "Connection lost, reconnecting" loop.

    Behaviour:
      * ``0.0.0.0`` / ``::`` / ``*``  → enumerate every non-loopback
        IPv4 address attached to the host, plus the system hostname
        (if it resolves to anything), plus ``localhost``/``127.0.0.1``.
        Order: public-looking IPs first (most useful for remote
        operators), hostname, then loopback (fallback for local dev).
      * Any other bind host (specific IP, a hostname) → return it
        as-is since the operator chose it deliberately.
    """
    is_wildcard = host in ("0.0.0.0", "::", "*", "")
    if not is_wildcard:
        return [host]

    import socket

    addrs: list[str] = []
    seen: set[str] = set()

    def _add(a: str) -> None:
        if a and a not in seen:
            seen.add(a)
            addrs.append(a)

    # Enumerate non-loopback IPv4 addresses from all interfaces.
    # getaddrinfo(hostname) pulls addresses via the resolver, which
    # covers most practical cases (WSL adapter, eth0, etc.).  We
    # deliberately skip IPv6 in the banner to keep it readable.
    try:
        hostname = socket.gethostname()
    except Exception:
        hostname = ""
    try:
        for info in socket.getaddrinfo(hostname or None, None, family=socket.AF_INET):
            ip = info[4][0]
            if ip and not ip.startswith("127.") and ip not in ("0.0.0.0",):
                _add(ip)
    except Exception:
        pass

    # Best-effort: also scan all configured interfaces via a UDP
    # connect trick — this catches interfaces that don't show up in
    # getaddrinfo(hostname), e.g. tunnels and secondary NICs.
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 53))
            _add(s.getsockname()[0])
    except Exception:
        pass

    # Add the hostname itself if it's distinct and not just
    # ``localhost``.  Users connecting from the same LAN may reach
    # the box by hostname (mDNS, /etc/hosts, corporate DNS).
    if hostname and hostname != "localhost":
        _add(hostname)

    # Loopback goes last — useful for local dev, useless for headless.
    _add("localhost")

    return addrs


def _db_ground_truth_counts(daemon: SwarmDaemon) -> dict[str, int] | None:
    """Query the DB directly for what it actually contains.

    Returns a dict with keys ``workers``, ``groups``, ``config``,
    ``global_rules``, ``worker_rules`` or ``None`` if the query fails.
    Used by the startup banner to detect silent config-load failures:
    if the in-memory state doesn't match what the DB holds, the user
    is running against a stale/fallback config and the dashboard will
    show empty panels regardless of what's on disk.
    """
    try:
        row = daemon.swarm_db.fetchone(
            "SELECT "
            "  (SELECT COUNT(*) FROM workers) AS w,"
            "  (SELECT COUNT(*) FROM groups) AS g,"
            "  (SELECT COUNT(*) FROM config WHERE key != 'update_cache') AS c,"
            "  (SELECT COUNT(*) FROM approval_rules WHERE owner_type='global') AS gr,"
            "  (SELECT COUNT(*) FROM approval_rules WHERE owner_type='worker') AS wr"
        )
    except Exception:
        return None
    if not row:
        return None
    return {
        "workers": row["w"] or 0,
        "groups": row["g"] or 0,
        "config": row["c"] or 0,
        "global_rules": row["gr"] or 0,
        "worker_rules": row["wr"] or 0,
    }


def _print_banner(daemon: SwarmDaemon, host: str, port: int) -> None:
    """Print NestJS-style structured startup banner."""
    import importlib.metadata

    try:
        version = importlib.metadata.version("swarm-ai")
    except importlib.metadata.PackageNotFoundError:
        version = "dev"

    Y = "\033[33m"  # yellow/honey
    C = "\033[36m"  # cyan
    D = "\033[2m"  # dim
    B = "\033[1m"  # bold
    M = "\033[31m"  # red — used for loud mismatch warnings
    R = "\033[0m"  # reset

    # Two distinct counts:
    #   n_configured = workers defined in the loaded config (DB/YAML)
    #   n_running    = live Worker objects whose PTY process is
    #                  currently attached via the holder
    # On a fresh ``swarm start`` with no prior launches, n_running is
    # 0 and n_configured is everything in swarm.db — that is NORMAL
    # and NOT a mismatch.  The old banner conflated these and cried
    # "MISMATCH" every single startup.
    n_running = len(daemon.workers)
    n_configured = len(daemon.config.workers)
    n_groups = len(daemon.config.groups)
    n_global_rules = len(daemon.config.drones.approval_rules)
    drones_enabled = daemon.pilot.enabled if daemon.pilot else False
    interval = daemon.config.drones.poll_interval
    queen_model = getattr(daemon.config.queen, "model", "sonnet")
    task_summary = daemon.task_board.summary()

    # Ground truth from the DB itself (independent of whatever the
    # loader actually installed on self.config).
    db_counts = _db_ground_truth_counts(daemon)
    config_source = getattr(daemon.config, "config_source", "unknown")

    from swarm.update import build_sha

    sha = build_sha()
    sha_suffix = f" @ {sha}" if sha else ""

    # Resolve a list of client-usable addresses.  Never display
    # 0.0.0.0 — it's a bind address, not a client address, and
    # Chrome's Private Network Access rules treat it specially which
    # causes the exact "Connection lost, reconnecting" loop users
    # have been hitting.  On headless servers we enumerate all
    # non-loopback interface IPs so remote operators see a URL they
    # can actually paste into a browser.
    addrs = _reachable_addresses(host)
    primary = addrs[0]
    extras = addrs[1:]

    print(f"\n{Y}{B}Swarm WUI v{version}{sha_suffix}{R}", flush=True)
    print(f"  {D}\u251c\u2500{R} Dashboard:  {C}http://{primary}:{port}{R}", flush=True)
    for extra in extras:
        print(f"  {D}\u2502{R}              {C}http://{extra}:{port}{R}", flush=True)
    print(f"  {D}\u251c\u2500{R} API:        {C}http://{primary}:{port}/api/health{R}", flush=True)
    print(f"  {D}\u251c\u2500{R} WebSocket:  {C}ws://{primary}:{port}/ws{R}", flush=True)

    # Config line — compares *configured* count against DB, not the
    # running count.  A MISMATCH here is a real bug (loader dropped
    # data).  The Workers line below shows running vs configured.
    source_label = {
        "db": "swarm.db",
        "yaml": "YAML fallback",
        "fresh": "fresh install (defaults)",
        "unknown": "unknown",
    }.get(config_source, config_source)
    loaded_summary = f"{n_configured} workers, {n_groups} groups, {n_global_rules} rules"
    if db_counts is not None and config_source == "db":
        db_summary = (
            f"{db_counts['workers']} workers, {db_counts['groups']} groups,"
            f" {db_counts['global_rules']} rules"
        )
        mismatch = (
            db_counts["workers"] != n_configured
            or db_counts["groups"] != n_groups
            or db_counts["global_rules"] != n_global_rules
        )
        if mismatch:
            print(
                f"  {D}\u251c\u2500{R} Config:     {M}{B}MISMATCH{R} "
                f"{source_label}  loaded={loaded_summary}  |  "
                f"db={db_summary}",
                flush=True,
            )
            print(
                f"  {D}\u2502{R}             {M}\u26a0 The daemon loader dropped data on the "
                f"way in. Re-run with --log-level DEBUG to see why.{R}",
                flush=True,
            )
        else:
            print(
                f"  {D}\u251c\u2500{R} Config:     {source_label} ({loaded_summary})",
                flush=True,
            )
    elif (
        config_source in {"yaml", "fresh"}
        and db_counts
        and any(db_counts[k] for k in ("workers", "groups", "global_rules", "worker_rules"))
    ):
        # Fell back to YAML/defaults but the DB actually has data — LOUD.
        print(
            f"  {D}\u251c\u2500{R} Config:     {M}{B}{source_label}{R}  loaded={loaded_summary}",
            flush=True,
        )
        print(
            f"  {D}\u2502{R}             {M}\u26a0 ~/.swarm/swarm.db contains "
            f"{db_counts['workers']} workers / {db_counts['global_rules']} rules "
            f"that are NOT loaded. Check log for DB load error.{R}",
            flush=True,
        )
    else:
        print(
            f"  {D}\u251c\u2500{R} Config:     {source_label} ({loaded_summary})",
            flush=True,
        )

    # Workers line shows running vs configured so "0 running" doesn't
    # look broken when the user just hasn't launched anything yet.
    if n_configured == 0:
        print(f"  {D}\u251c\u2500{R} Workers:    {Y}0{R} configured", flush=True)
    elif n_running == n_configured:
        print(
            f"  {D}\u251c\u2500{R} Workers:    {Y}{n_running}{R} running "
            f"({Y}{n_configured}{R} configured)",
            flush=True,
        )
    else:
        # Partial or no workers launched yet — normal on a fresh start.
        print(
            f"  {D}\u251c\u2500{R} Workers:    {Y}{n_running}{R} running, "
            f"{Y}{n_configured}{R} configured  "
            f"{D}(run `swarm launch -a` to start them){R}",
            flush=True,
        )
    drones_str = f"enabled (interval {interval}s)" if drones_enabled else "disabled"
    print(f"  {D}\u251c\u2500{R} Drones:     {drones_str}", flush=True)
    print(f"  {D}\u251c\u2500{R} Queen:      ready (model: {queen_model})", flush=True)
    # Auth status
    explicit_pw = os.environ.get("SWARM_API_PASSWORD") or daemon.config.api_password
    if explicit_pw:
        print(f"  {D}\u251c\u2500{R} Auth:       explicit password set", flush=True)
    else:
        from swarm.server.api import _auto_token

        print(
            f"  {D}\u251c\u2500{R} Auth:       auto-token {Y}{_auto_token[:12]}…{R}"
            f" (set SWARM_API_PASSWORD for persistent auth)",
            flush=True,
        )
    # Check cache-only for update info (no network call during startup)
    from swarm.update import check_for_update_sync

    cached = check_for_update_sync()
    if cached and cached.available:
        print(
            f"  {D}\u251c\u2500{R} Tasks:      {task_summary}",
            flush=True,
        )
        print(
            f"  {D}\u2514\u2500{R} Update:     {Y}{cached.remote_version}{R} available"
            f" (current: {cached.current_version})",
            flush=True,
        )
    else:
        print(f"  {D}\u2514\u2500{R} Tasks:      {task_summary}", flush=True)
    print(flush=True)


async def run_test_daemon(
    config: HiveConfig, host: str = "0.0.0.0", port: int | None = None, timeout: int = 300
) -> Path | None:
    """Run the daemon in test mode with auto-shutdown on completion or timeout.

    Returns the report file path, or None if no report was generated.
    Raises TimeoutError if the timeout is reached.
    """
    import signal

    from swarm.server.api import create_app

    port = port or config.test.port

    # Isolate test tasks from the main task board so they don't leak.
    test_store = FileTaskStore(path=Path.home() / ".swarm" / "test-tasks.json")
    daemon = SwarmDaemon(config, task_store=test_store)

    from swarm.pty.pool import ProcessPool

    pool = ProcessPool()
    await pool.ensure_holder()
    daemon.pool = pool

    await daemon.start()
    daemon._init_test_mode()

    app = create_app(daemon)

    shutdown = asyncio.Event()
    app["shutdown_event"] = shutdown
    report_result: dict[str, Path | None] = {"path": None}

    # Hook into broadcast_ws to detect test_report_ready
    def _on_ws_broadcast(data: dict[str, Any]) -> None:
        if data.get("type") == "test_report_ready":
            report_result["path"] = Path(data["path"])
            shutdown.set()

    daemon._broadcast_hook = _on_ws_broadcast

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()

    _print_test_banner(daemon, host, port, timeout)
    _wire_test_console(daemon)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, shutdown.set)

    timed_out = False
    try:
        await asyncio.wait_for(shutdown.wait(), timeout=timeout)
    except TimeoutError:
        timed_out = True
        console_log(f"Test timeout reached ({timeout}s)", level="warn")

    # If we timed out without a report, try to generate one as fallback
    if timed_out and report_result["path"] is None:
        await daemon._generate_test_report_if_pending()
        # Check if the fallback produced a report via the test_log
        if daemon.test_runner.test_log is not None:
            report_dir = Path(daemon.test_runner.test_log.report_dir)
            # Find the most recent report
            reports = sorted(report_dir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
            if reports:
                report_result["path"] = reports[0]

    print("\nShutting down test daemon...", flush=True)
    await daemon.stop()
    await runner.cleanup()

    if timed_out and report_result["path"] is None:
        raise TimeoutError(f"Test timed out after {timeout}s with no report")

    return report_result["path"]


def _wire_test_console(daemon: SwarmDaemon) -> None:
    """Wire pilot + daemon events to console_log with structured prefixes."""
    if daemon.pilot:
        daemon.pilot.on_state_changed(lambda w: console_log(f"[STATE] {w.name} -> {w.state.value}"))
        daemon.pilot.on_task_assigned(
            lambda w, t, m="": console_log(f'[TASK] "{t.title}" -> {w.name}')
        )
        daemon.pilot.on_workers_changed(lambda: console_log("[HIVE] Workers changed"))
        daemon.pilot.on_hive_empty(lambda: console_log("[HIVE] All workers gone", level="warn"))
        daemon.pilot.on_hive_complete(lambda: console_log("[HIVE] Complete — all tasks done"))

        # Drone decisions (skip NONE to reduce noise)
        if hasattr(daemon.pilot, "_emit_decisions"):
            daemon.pilot.on(
                "drone_decision",
                lambda w, content, d: (
                    console_log(f"[DRONE] {w.name}: {d.decision.value} — {d.reason}")
                    if d.decision != Decision.NONE
                    else None
                ),
            )

        daemon.pilot.on_escalate(
            lambda w, reason: console_log(f"[ESCALATE] {w.name}: {reason}", level="warn")
        )

    # Queen analysis events
    daemon.on(
        "queen_analysis",
        lambda wn, action, reasoning, conf: console_log(
            f"[QUEEN] {wn}: {action} (confidence={conf:.2f})"
        ),
    )

    daemon.task_board.on_change(lambda: console_log("[TASK] Board updated"))


def _print_test_banner(daemon: SwarmDaemon, host: str, port: int, timeout: int) -> None:
    """Print structured startup banner for test mode."""
    import importlib.metadata

    try:
        version = importlib.metadata.version("swarm-ai")
    except importlib.metadata.PackageNotFoundError:
        version = "dev"

    Y = "\033[33m"
    C = "\033[36m"
    D = "\033[2m"
    B = "\033[1m"
    R = "\033[0m"

    n_workers = len(daemon.workers)
    n_tasks = len(daemon.task_board.all_tasks)
    session = daemon.config.session_name

    # Same 0.0.0.0 → reachable-address treatment as the main banner.
    _test_addrs = _reachable_addresses(host)
    _primary = _test_addrs[0]
    print(f"\n{Y}{B}Swarm Test Runner v{version}{R}", flush=True)
    print(f"  {D}\u251c\u2500{R} Dashboard:  {C}http://{_primary}:{port}{R}", flush=True)
    for _extra in _test_addrs[1:]:
        print(f"  {D}\u2502{R}              {C}http://{_extra}:{port}{R}", flush=True)
    print(f"  {D}\u251c\u2500{R} Workers:    {Y}{n_workers}{R} test worker(s)", flush=True)
    print(f"  {D}\u251c\u2500{R} Tasks:      {Y}{n_tasks}{R} loaded", flush=True)
    print(f"  {D}\u251c\u2500{R} Timeout:    {timeout}s", flush=True)
    print(f"  {D}\u251c\u2500{R} Session:    {session}", flush=True)
    print(f"  {D}\u2514\u2500{R} Port:       {port}", flush=True)
    print(flush=True)


_console_pipe_broken = False


def console_log(msg: str, level: str = "info") -> None:
    """Print a timestamped runtime event to the console.

    Silently stops logging after the first BrokenPipeError — the parent
    terminal is gone and further attempts would just flood the error log.
    """
    global _console_pipe_broken

    from datetime import datetime

    ts = datetime.now().strftime("%H:%M:%S")
    if level == "warn":
        prefix = "\033[33m\u26a0\033[0m"
    elif level == "error":
        prefix = "\033[31m\u2717\033[0m"
    else:
        prefix = " "
    try:
        print(f"[{ts}] {prefix} {msg}", flush=True)
        _console_pipe_broken = False
    except BrokenPipeError:
        if not _console_pipe_broken:
            _console_pipe_broken = True
