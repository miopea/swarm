"""ConfigManager — config validation, hot-reload, and persistence.

Per-section validators (``apply_drones``, ``apply_queen``,
``apply_notifications``, …) live in :mod:`swarm.server.config_appliers`.
This module hosts:

* The module-level dataclass dispatch helpers
  (:func:`_apply_dataclass_dict`, :func:`validate_body_keys`,
  :func:`_apply_typed_value`, etc.) that the appliers use as their
  framework.
* :class:`FieldOutcome` / :class:`ApplyResult` result types.
* :class:`ConfigManager` itself — now a thin coordinator that wires
  the appliers together via :data:`SECTION_REGISTRY`.

See ``docs/specs/config-manager-refactor.md`` for the extraction
spec.  Pre-refactor this file was 1584 lines / 41 methods; post-
refactor it's around 500 / 12 because the per-section validation
logic moved out.
"""

from __future__ import annotations

import asyncio
import typing
from collections.abc import Callable
from dataclasses import dataclass, field, is_dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, get_args, get_origin

from yaml import YAMLError

from swarm.config import DroneApprovalRule, HiveConfig, load_config, save_config
from swarm.drones.log import DroneLog, LogCategory, SystemAction
from swarm.logging import get_logger


@dataclass
class FieldOutcome:
    """Per-section outcome from a config apply pass.

    Phase 7 of #328.  Captures what landed in a single dataclass
    section so the operator can see field-level success/failure
    without scraping the server log.
    """

    consumed: list[str] = field(default_factory=list)
    """Field names that were validated and applied to the target."""
    unknown: list[str] = field(default_factory=list)
    """Body keys that didn't match any field on the dataclass — drift."""

    def to_dict(self) -> dict[str, list[str]]:
        return {"consumed": list(self.consumed), "unknown": list(self.unknown)}


@dataclass
class ApplyResult:
    """Aggregate outcome of an ``apply_update`` call.

    ``consumed`` and ``unknown`` are the union of all per-section
    outcomes plus top-level body keys.  ``sections`` keeps the
    per-section breakdown so the dashboard can render
    "drones: applied 3, ignored 1; queen: applied 1" style summaries.
    """

    consumed: list[str] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)
    sections: dict[str, FieldOutcome] = field(default_factory=dict)

    def merge_section(self, name: str, outcome: FieldOutcome) -> None:
        self.sections[name] = outcome
        for k in outcome.consumed:
            self.consumed.append(f"{name}.{k}")
        for k in outcome.unknown:
            self.unknown.append(f"{name}.{k}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "consumed": list(self.consumed),
            "unknown": list(self.unknown),
            "sections": {n: o.to_dict() for n, o in self.sections.items()},
        }


if TYPE_CHECKING:
    from swarm.db.core import SwarmDB
    from swarm.drones.pilot import DronePilot
    from swarm.server.worker_service import WorkerService

_log = get_logger("server.config_manager")


# Cache resolved type hints per dataclass — resolution is non-trivial
# (string annotations + ForwardRef) and these classes don't change at
# runtime.  Keeps the generic applier hot-path cheap.
_TYPE_HINTS_CACHE: dict[type, dict[str, Any]] = {}


def _resolve_hints(cls: type) -> dict[str, Any]:
    if cls not in _TYPE_HINTS_CACHE:
        _TYPE_HINTS_CACHE[cls] = typing.get_type_hints(cls)
    return _TYPE_HINTS_CACHE[cls]


def _apply_scalar(target: object, key: str, value: object, t: type, label: str) -> None:
    """Validate scalar ``value`` against primitive type ``t`` and assign."""
    # bool checked before int because bool is a subclass of int
    if t is bool:
        if not isinstance(value, bool):
            raise ValueError(f"{label} must be boolean")
        setattr(target, key, value)
    elif t is int:
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(f"{label} must be an integer")
        setattr(target, key, value)
    elif t is float:
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ValueError(f"{label} must be a number")
        setattr(target, key, float(value))
    elif t is str:
        if not isinstance(value, str):
            raise ValueError(f"{label} must be a string")
        setattr(target, key, value)
    else:
        raise ValueError(f"{label} ({t!r}) is not generic-applicable")


def _apply_collection(
    target: object,
    key: str,
    value: object,
    expected_type: Any,
    label: str,
) -> None:
    """Validate list[str] / dict[str, str] ``value`` and assign."""
    origin = get_origin(expected_type)
    if origin is list:
        args = get_args(expected_type)
        if not isinstance(value, list):
            raise ValueError(f"{label} must be a list")
        if args and args[0] is str:
            if not all(isinstance(e, str) for e in value):
                raise ValueError(f"{label} must be a list of strings")
            setattr(target, key, list(value))
            return
        raise ValueError(f"{label} (list of {args[0] if args else '?'}) is not generic-applicable")
    if origin is dict:
        if not isinstance(value, dict):
            raise ValueError(f"{label} must be an object")
        setattr(target, key, {str(k): str(v) for k, v in value.items()})
        return
    raise ValueError(f"{label} ({expected_type!r}) is not a supported collection")


def _apply_typed_value(
    target: object,
    key: str,
    value: object,
    expected_type: Any,
    section_name: str,
) -> None:
    """Validate ``value`` against ``expected_type`` and assign to target.

    Handles the primitive shapes the config dashboard actually sends:
    bool, int, float, str, list[str], dict[str, str], and nested
    dataclasses (recursive apply).  More complex types (e.g.
    ``list[DroneApprovalRule]``) are out of scope for this generic
    path — the caller declares them in ``skip_keys`` and validates
    by hand.
    """
    label = f"{section_name}.{key}"
    origin = get_origin(expected_type)

    # Nested dataclass — recurse
    if is_dataclass(expected_type):
        if not isinstance(value, dict):
            raise ValueError(f"{label} must be an object")
        nested = getattr(target, key)
        _apply_dataclass_dict(value, nested, label)
        return

    # list[X] / dict[X, Y]
    if origin in (list, dict):
        _apply_collection(target, key, value, expected_type, label)
        return

    # Optional / Union — extract single non-None member.
    if origin is typing.Union:
        args = [a for a in get_args(expected_type) if a is not type(None)]
        if len(args) == 1:
            _apply_typed_value(target, key, value, args[0], section_name)
            return
        raise ValueError(f"{label} ({expected_type!r}) is not generic-applicable")

    # Plain primitive type
    if isinstance(expected_type, type):
        _apply_scalar(target, key, value, expected_type, label)
        return

    raise ValueError(f"{label} ({expected_type!r}) is not generic-applicable")


def _warn_unknown_subkeys(body: dict[str, Any], cls: type, section_name: str) -> None:
    """Log WARNING for any body key not present on the section's dataclass.

    Lightweight defensive sweep for handlers that already cover their
    fields via custom validation — no auto-apply, just a drift signal.
    Phase 3 of the multi-phase #328 fix.
    """
    declared = set(_resolve_hints(cls).keys())
    for key in body:
        if key not in declared:
            _log.warning(
                "%s: ignoring unknown sub-key %r — dashboard/server schema drift; "
                "data will not persist",
                section_name,
                key,
            )


def validate_body_keys(
    body: dict[str, Any],
    expected: set[str],
    section_name: str,
) -> FieldOutcome:
    """Validate body keys against a fixed expected set.

    Sister of ``_apply_dataclass_dict`` for endpoints whose bodies
    aren't dataclass-shaped (e.g. ``/workers/{name}/add-to-group``
    takes ``{group, create}``).  Returns a FieldOutcome with consumed
    = body keys present in ``expected`` and unknown = the rest.
    Logs WARNING for each unknown key, mirroring the dispatch helper's
    drift signal.  Phase 8 of #328.
    """
    body_keys = set(body)
    consumed = sorted(body_keys & expected)
    unknown = sorted(body_keys - expected)
    if unknown:
        _log.warning(
            "%s: ignoring unknown body key(s) %s — dashboard/server schema drift; "
            "data will not persist",
            section_name,
            unknown,
        )
    return FieldOutcome(consumed=consumed, unknown=unknown)


def _apply_dataclass_dict(
    body: dict[str, Any],
    target: object,
    section_name: str,
    skip_keys: set[str] | None = None,
) -> FieldOutcome:
    """Apply ``body`` onto a dataclass ``target`` by introspection.

    Returns a ``FieldOutcome`` listing which keys were applied
    (``consumed``) and which the dataclass didn't recognize
    (``unknown``).  Any body key whose name does not match a
    dataclass field — and isn't listed in ``skip_keys`` — is logged
    at WARNING and ignored.

    Phase 7 of #328 changed the return type from ``set[str]`` to
    ``FieldOutcome`` so callers can plumb per-field outcomes back to
    the operator (HTTP response, dashboard toast).  ``skip_keys``
    entries are silently skipped — no consume, no unknown — so the
    caller can list its own custom-validated fields without polluting
    the structured outcome.

    Phase 3 originally landed this helper to replace the cherry-pick
    pattern in per-section ``_apply_X`` handlers with type-driven
    dispatch from ``__dataclass_fields__``.  Adding a field to a
    dataclass is now sufficient — no manual entry in a per-section
    allow-list, no missed hand-off.
    """
    if not is_dataclass(target):
        raise TypeError(f"{section_name}: target must be a dataclass instance")
    skip = skip_keys or set()
    cls = type(target)
    hints = _resolve_hints(cls)
    declared = set(hints.keys())
    outcome = FieldOutcome()

    for key, value in body.items():
        if key in skip:
            # Caller is responsible for this key.  Don't warn, don't apply.
            continue
        if key not in declared:
            _log.warning(
                "%s: ignoring unknown sub-key %r — dashboard/server schema drift; "
                "data will not persist",
                section_name,
                key,
            )
            outcome.unknown.append(key)
            continue
        _apply_typed_value(target, key, value, hints[key], section_name)
        outcome.consumed.append(key)
    return outcome


def _body_touches_approval_rules(body: dict[str, Any]) -> bool:
    """Return True if an apply_update body contains an approval_rules edit.

    Checks both the global path (``body["drones"]["approval_rules"]``)
    and any per-worker path
    (``body["workers"][i]["approval_rules"]``).  Used to decide whether
    the subsequent save should propagate ``sync_rules=True`` — keeping
    routine non-rules edits from overwriting the approval_rules table.
    """
    drones = body.get("drones")
    if isinstance(drones, dict) and "approval_rules" in drones:
        return True
    workers = body.get("workers")
    if isinstance(workers, list):
        for w in workers:
            if isinstance(w, dict) and "approval_rules" in w:
                return True
    return False


class ConfigManager:
    """Coordinates config hot-reload, validation, and persistence.

    Validation / apply-by-section logic lives in
    :mod:`swarm.server.config_appliers`; this class wires those
    appliers (via :data:`~swarm.server.config_appliers.SECTION_REGISTRY`)
    around the lifecycle pieces — file mtime watch, hot-reload, save
    to DB / YAML — that stay here because they're the manager's
    responsibility.
    """

    def __init__(
        self,
        config: HiveConfig,
        broadcast_ws: Callable[[dict[str, Any]], None],
        drone_log: DroneLog,
        apply_config: Callable[[], None],
        get_pilot: Callable[[], DronePilot | None],
        rebuild_graph: Callable[[], None],
        rebuild_jira: Callable[[], None] | None = None,
        get_worker_svc: Callable[[], WorkerService | None] | None = None,
        swarm_db: SwarmDB | None = None,
    ) -> None:
        self._config = config
        self._broadcast_ws = broadcast_ws
        self._drone_log = drone_log
        self._apply_config = apply_config
        self._get_pilot = get_pilot
        self._rebuild_graph = rebuild_graph
        self._rebuild_jira = rebuild_jira or (lambda: None)
        self._get_worker_svc = get_worker_svc or (lambda: None)
        self._swarm_db = swarm_db  # SwarmDB instance (None = YAML-only)
        self._config_mtime: float = 0.0
        # Built on first apply_update so config_appliers can import
        # FieldOutcome / _apply_dataclass_dict from this module
        # without circular-importing the registry at module load.
        # Late-bound on first apply_update so config_appliers can import
        # FieldOutcome / _apply_dataclass_dict from this module without
        # circular-importing the registry at module load. See _build_deps.
        self._deps: Any = None

    # --- Hot-reload ---

    def hot_apply(self) -> None:
        """Apply config changes to pilot, queen, and notification bus."""
        self._apply_config()

    def _invalidate_provider_cache(self) -> None:
        """Clear pilot's provider cache so tuning changes take effect."""
        pilot = self._get_pilot()
        if pilot:
            pilot.invalidate_provider_cache()

    async def reload(self, new_config: HiveConfig) -> None:
        """Hot-reload configuration. Updates pilot, queen, and notifies WS clients."""
        # Replace the shared config object's fields in-place so all holders
        # of the reference see the update.  The daemon's self.config binding
        # is updated by the caller (apply_update) when needed.
        self._config.__dict__.update(new_config.__dict__)
        self.hot_apply()

        # Update mtime tracker
        if new_config.source_path:
            sp = Path(new_config.source_path)
            if sp.exists():
                self._config_mtime = sp.stat().st_mtime

        self._broadcast_ws({"type": "config_changed"})
        self._drone_log.add(
            SystemAction.CONFIG_CHANGED,
            "system",
            "config reloaded",
            category=LogCategory.SYSTEM,
        )
        _log.info("config hot-reloaded")

    async def watch_mtime(self) -> None:
        """Poll config file mtime every 30s and notify WS clients if changed."""
        try:
            while True:
                await asyncio.sleep(30)
                if not self._config.source_path:
                    continue
                try:
                    sp = Path(self._config.source_path)
                    if sp.exists():
                        mtime = sp.stat().st_mtime
                        if mtime > self._config_mtime:
                            self._config_mtime = mtime
                            self._broadcast_ws({"type": "config_file_changed"})
                            _log.info("config file changed on disk")
                except OSError:
                    _log.debug("mtime check failed", exc_info=True)
        except asyncio.CancelledError:
            return

    def check_file(self) -> bool:
        """Check if config file changed on disk; reload if so. Returns True if reloaded."""
        if not self._config.source_path:
            return False
        try:
            current_mtime = Path(self._config.source_path).stat().st_mtime
        except OSError:
            return False
        if current_mtime <= self._config_mtime:
            return False
        self._config_mtime = current_mtime
        try:
            new_config = load_config(self._config.source_path)
        except (OSError, ValueError, KeyError, YAMLError):
            _log.warning("failed to reload config from disk", exc_info=True)
            return False

        # Hot-apply fields that don't require worker lifecycle changes.
        #
        # CAREFUL: approval_rules live in the DB, not the YAML.  The
        # YAML hot-reload must NOT overwrite them — if we blindly
        # assigned ``self._config.drones = new_config.drones`` the
        # in-memory rule list would be wiped on every external YAML
        # edit (user tweaks a scalar in swarm.yaml → rule list goes
        # empty → dashboard shows nothing).  Preserve the existing
        # rules and copy the YAML-editable drone fields in place.
        preserved_global_rules = list(self._config.drones.approval_rules)
        # Preserve per-worker rules too, keyed by worker name.
        preserved_worker_rules = {w.name: list(w.approval_rules) for w in self._config.workers}

        # Groups live in the DB in DB-first mode (#328).  If the YAML
        # on disk lacks a groups section (or carries an empty one),
        # don't overwrite the in-memory list — that would wipe the
        # operator's dashboard-managed groups on every external scalar
        # edit.  Same preservation pattern as approval_rules above.
        if new_config.groups:
            self._config.groups = new_config.groups
        # else: keep self._config.groups as-is (DB-sourced)
        new_config.drones.approval_rules = preserved_global_rules
        self._config.drones = new_config.drones
        self._config.queen = new_config.queen
        self._config.notifications = new_config.notifications
        # Reapply preserved per-worker rules onto the reloaded worker
        # list before we swap it in.
        for w in new_config.workers:
            if w.name in preserved_worker_rules:
                w.approval_rules = preserved_worker_rules[w.name]
        self._config.workers = new_config.workers
        self._config.api_password = new_config.api_password
        self._config.test = new_config.test
        self._config.custom_llms = new_config.custom_llms
        self._config.provider_overrides = new_config.provider_overrides
        # Refresh custom provider registry from disk-reloaded config
        if new_config.custom_llms:
            from swarm.providers import register_custom_providers

            register_custom_providers(new_config.custom_llms)
        from swarm.providers import register_provider_overrides

        register_provider_overrides(new_config.provider_overrides)
        self._invalidate_provider_cache()

        self.hot_apply()

        _log.info("config reloaded from disk (external change detected)")
        return True

    def toggle_drones(self) -> bool:
        """Toggle drone pilot and persist to config. Returns new enabled state."""
        pilot = self._get_pilot()
        if not pilot:
            return False
        new_state = pilot.toggle()
        self._config.drones.enabled = new_state
        self.save()
        self._broadcast_ws({"type": "drones_toggled", "enabled": new_state})
        return new_state

    def save(self, *, sync_rules: bool = False) -> None:
        """Save config to DB (primary) or YAML (fallback).

        Pass ``sync_rules=True`` **only** when the caller has just
        modified ``drones.approval_rules`` (global or per-worker) and
        wants the change persisted.  Default is False so routine saves
        — toggling drones, editing unrelated settings, hot-reload
        callbacks — cannot wipe the rules table.
        """
        if self._save_to_db(sync_rules=sync_rules):
            return
        from swarm.config import ConfigError

        try:
            save_config(self._config)
        except ConfigError:
            return
        if self._config.source_path:
            try:
                self._config_mtime = Path(self._config.source_path).stat().st_mtime
            except OSError:
                pass

    def _save_to_db(self, *, sync_rules: bool = False) -> bool:
        """Save config to swarm.db. Returns True on success.

        Failures are logged at WARNING level (not DEBUG) so a silently
        failing save surfaces in default-level operator logs.  Reported
        in #328: a user's Groups edits weren't persisting across reboots
        because the dashboard reported success while the underlying DB
        write was failing — with no forensic evidence at WARNING.
        """
        if self._swarm_db is None:
            return False
        try:
            from swarm.db.config_store import save_config_to_db

            save_config_to_db(self._swarm_db, self._config, sync_approval_rules=sync_rules)
            return True
        except Exception:
            _log.warning("DB config save failed", exc_info=True)
            return False

    # --- Config update validation + apply ---

    @staticmethod
    def parse_approval_rules(rules_raw: object) -> list[DroneApprovalRule]:
        """Parse and validate approval rules from a config update.

        Thin pass-through to :func:`config_appliers.parse_approval_rules`
        so existing static callers (``ConfigManager.parse_approval_rules(raw)``)
        keep working after the section appliers moved.
        """
        from swarm.server.config_appliers import parse_approval_rules

        return parse_approval_rules(rules_raw)

    def _build_deps(self) -> Any:
        """Build the ApplierDeps bundle on demand.

        Lazy so ``swarm.server.config_appliers`` can import from this
        module at top-level without circling back through here at
        ConfigManager construction time.
        """
        if self._deps is None:
            from swarm.server.config_appliers import ApplierDeps

            self._deps = ApplierDeps(
                invalidate_provider_cache=self._invalidate_provider_cache,
                get_worker_svc=self._get_worker_svc,
            )
        return self._deps

    # ----- backward-compat shims for tests that patch the old method names -----
    #
    # Tests in tests/test_config_manager.py historically reached into
    # ``ConfigManager._apply_workflows`` via ``patch(...)`` to verify
    # dispatch wiring.  After the refactor the real logic lives in
    # :mod:`swarm.server.config_appliers.workflows`; the shim below
    # preserves the old import path so those tests keep working without
    # the test file being rewritten in lock-step.

    def _apply_workflows(self, wf: object) -> None:
        """Backward-compat shim — delegate to the workflows applier."""
        from swarm.server.config_appliers.workflows import apply_workflows

        apply_workflows(self._config, wf, deps=self._build_deps())

    def _apply_drones(self, bz: dict[str, Any]) -> FieldOutcome:
        """Backward-compat shim — delegate to the drones applier."""
        from swarm.server.config_appliers.drones import apply_drones

        return apply_drones(self._config, bz, deps=self._build_deps())

    def _apply_scalars(self, body: dict[str, Any]) -> None:
        """Backward-compat shim — delegate to the scalars applier.

        The legacy method returned ``None``; the new applier returns a
        ``FieldOutcome`` for the registry to aggregate.  Discarding it
        here preserves the call signature tests expect.
        """
        from swarm.server.config_appliers.workers import apply_scalars

        apply_scalars(self._config, body, deps=self._build_deps())

    def _apply_workers(self, workers: dict[str, Any]) -> None:
        """Backward-compat shim — delegate to the workers applier.

        Some integration tests call this method directly with the
        ``{worker_name: worker_data}`` mapping that the legacy method
        consumed; preserve the signature so they keep working.
        """
        from swarm.server.config_appliers.workers import _apply_workers

        _apply_workers(self._config, workers, deps=self._build_deps())

    async def apply_update(self, body: dict[str, Any]) -> dict[str, Any]:
        """Apply a partial config update from the API.

        Returns a ``dict`` representation of the structured ``ApplyResult``
        — consumed and unknown keys per section, plus top-level unknowns —
        so the HTTP route can surface them to the operator.  Phase 7 of
        #328: dashboard now shows "Saved 5 fields, 1 unknown ignored"
        instead of a bare "Saved" toast.

        Raises ``ValueError`` on type / range mismatch (still caught by
        ``handle_errors`` and returned as 400).
        """
        from swarm.server.config_appliers import (
            SECTION_REGISTRY,
            VIRTUAL_APPLIERS,
            known_body_keys,
        )

        # Diagnostic: surface the body shape and current workflows state
        # at every dispatch.  Triages "workflows reset to empty" symptoms
        # by anchoring exactly which save mutated the in-memory dict
        # (Amanda 2026-05-05 — workflows lost across restart even though
        # DB row + load_config_from_db both verify correct).
        _log.info(
            "apply_update: body_keys=%s body.workflows=%r pre.cfg.workflows=%r",
            sorted(body.keys()),
            body.get("workflows"),
            self._config.workflows,
        )
        result = ApplyResult()
        deps = self._build_deps()

        for section_name, applier, merge in SECTION_REGISTRY:
            if section_name not in body:
                continue
            # Special case: the ``workflows`` applier still flows
            # through ``self._apply_workflows`` so the legacy test
            # patch path (``patch.object(ConfigManager, "_apply_workflows")``)
            # observes the call.  Behaviour-identical to calling the
            # applier directly.
            if section_name == "workflows":
                self._apply_workflows(body["workflows"])
                continue
            outcome = applier(self._config, body[section_name], deps=deps)
            if merge:
                result.merge_section(section_name, outcome)

        # Virtual sections — their keys live at the top level of the
        # body rather than under a section name.
        for section_name, applier in VIRTUAL_APPLIERS:
            outcome = applier(self._config, body, deps=deps)
            # Both ``advanced`` and ``scalars`` promote their consumed
            # keys to the top-level list (no per-section nesting).
            result.consumed.extend(outcome.consumed)

        # Phase 2 fail-loud guard (#328): any top-level key the body
        # carried but no handler consumed gets logged at WARNING.  The
        # save still proceeds — this is forensic, not blocking — so a
        # client speaking a slightly newer schema doesn't get its
        # legitimate edits rejected wholesale.  The signal lives in
        # ``~/.swarm/swarm.log`` so future Amanda-class symptoms ("I
        # typed it but it didn't save") have a single place to look.
        #
        # The known-keys set is now derived from the registry
        # (``known_body_keys()``) rather than a hand-maintained
        # frozenset; adding a section automatically extends the
        # allow-list.
        unknown = sorted(set(body) - known_body_keys())
        if unknown:
            _log.warning(
                "apply_update: ignoring unknown config key(s) %s — "
                "dashboard/server schema drift; data will not persist",
                unknown,
            )
            result.unknown.extend(unknown)

        # Rebuild integration managers if credentials changed
        self._rebuild_graph()
        self._rebuild_jira()

        # Hot-reload and save.  Only forward sync_rules=True when the
        # caller's payload genuinely carried an approval_rules update
        # (global or per-worker), so an unrelated config edit can't
        # cascade into rewriting the approval_rules table.
        rules_touched = _body_touches_approval_rules(body)
        await self.reload(self._config)
        self.save(sync_rules=rules_touched)
        _log.info(
            "apply_update: post.cfg.workflows=%r sync_rules=%s",
            self._config.workflows,
            rules_touched,
        )
        return result.to_dict()
