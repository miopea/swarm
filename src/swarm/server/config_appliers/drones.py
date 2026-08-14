"""``drones`` section applier — pilot tuning, state thresholds, approval rules."""

from __future__ import annotations

import re as _re
from typing import TYPE_CHECKING, Any

from swarm.config import DroneApprovalRule
from swarm.config.models import StateThresholds
from swarm.server.config_manager import FieldOutcome, _apply_dataclass_dict

if TYPE_CHECKING:
    from swarm.config import HiveConfig
    from swarm.server.config_appliers._base import ApplierDeps


# Drone keys that need bespoke validation (range checks, regex
# compile, nested dataclass, custom dataclass list).  Generic dispatch
# skips these; ``apply_drones`` handles them by hand before delegating
# the rest of the section.
_CUSTOM_KEYS: frozenset[str] = frozenset(
    {
        "state_thresholds",  # nested dataclass with range checks
        "approval_rules",  # list[DroneApprovalRule] with regex compile
        "allowed_read_paths",  # legacy lenient parsing
    }
)

# Drone numeric fields that must be non-negative.  Pre-validated with
# explicit error messages before generic dispatch so the existing API
# contract — "drones.X must be >= 0" — is preserved regardless of
# which scalar drift catches the value.
_NON_NEGATIVE_NUMBERS: frozenset[str] = frozenset(
    {
        "escalation_threshold",
        "poll_interval",
        "max_revive_attempts",
        "max_poll_failures",
        "max_idle_interval",
        "idle_assign_threshold",
        "auto_complete_min_idle",
        "sleeping_threshold",
        "inv2_absent_threshold_seconds",
        "sleeping_poll_interval",
        "stung_reap_timeout",
        "poll_interval_buzzing",
        "poll_interval_waiting",
        "poll_interval_resting",
        "context_warning_threshold",
        "context_critical_threshold",
        "idle_nudge_interval_seconds",
        "idle_nudge_debounce_seconds",
        "idle_nudge_activity_window_seconds",
        "assign_affinity_floor",
        "assign_operator_engagement_minutes",
    }
)


def parse_approval_rules(rules_raw: object) -> list[DroneApprovalRule]:
    """Parse and validate approval rules from a config update."""
    if not isinstance(rules_raw, list):
        raise ValueError("drones.approval_rules must be a list")
    parsed = []
    for i, r in enumerate(rules_raw):
        if not isinstance(r, dict):
            raise ValueError(f"drones.approval_rules[{i}] must be an object")
        pattern = r.get("pattern", "")
        action = r.get("action", "approve")
        if action not in ("approve", "escalate"):
            raise ValueError(f"drones.approval_rules[{i}].action must be 'approve' or 'escalate'")
        try:
            _re.compile(pattern)
        except _re.error as exc:
            raise ValueError(f"drones.approval_rules[{i}].pattern: invalid regex: {exc}") from exc
        parsed.append(DroneApprovalRule(pattern=pattern, action=action))
    return parsed


def _apply_drone_state_thresholds(cfg_st: StateThresholds, st: dict[str, Any]) -> None:
    """Validate and apply drones.state_thresholds sub-section."""
    for k in ("buzzing_confirm_count", "stung_confirm_count"):
        if k in st:
            v = st[k]
            if not isinstance(v, int) or v < 1:
                raise ValueError(f"drones.state_thresholds.{k} must be >= 1")
            setattr(cfg_st, k, v)
    if "revive_grace" in st:
        v = st["revive_grace"]
        if not isinstance(v, (int, float)) or v < 0:
            raise ValueError("drones.state_thresholds.revive_grace must be >= 0")
        cfg_st.revive_grace = float(v)


def _validate_drone_ranges(bz: dict[str, Any]) -> None:
    """Pre-validate range constraints for known numeric drone fields.

    Raises ValueError with the explicit ``drones.X must be >= 0``
    message that the API contract guarantees.  Type validation happens
    later in the generic dispatch — but operators expect the range
    error to win for negative inputs.
    """
    for key in _NON_NEGATIVE_NUMBERS:
        if key not in bz:
            continue
        val = bz[key]
        if not isinstance(val, (int, float)) or isinstance(val, bool):
            raise ValueError(f"drones.{key} must be a number")
        if val < 0:
            raise ValueError(f"drones.{key} must be >= 0")


def apply_drones(
    cfg: HiveConfig,
    body: dict[str, Any],
    *,
    deps: ApplierDeps,  # protocol-uniform; drones doesn't use it
) -> FieldOutcome:
    """Validate and apply the ``drones`` section of a config update.

    Generic dataclass dispatch (Phase 3 of #328) handles every
    primitive scalar declared on ``DroneConfig`` — including fields
    that previously had to be hand-listed in ``_DRONE_SCALAR_KEYS``
    (and were silently dropped when someone forgot, e.g.
    ``context_warning_threshold``, ``speculation_enabled``,
    ``idle_nudge_*``).  Adding a field to ``DroneConfig`` is now
    sufficient — no per-section allow-list maintenance.

    Three keys still need bespoke validation:
    ``state_thresholds`` (nested dataclass with range checks),
    ``approval_rules`` (list of dataclass with regex compile), and
    ``allowed_read_paths`` (legacy lenient parsing).  Range
    constraints (non-negative numbers) are pre-validated to preserve
    the existing ``drones.X must be >= 0`` error contract.
    """
    dc = cfg.drones
    # 1. Range pre-validation for non-negative numeric fields
    #    (preserves the explicit "must be >= 0" error contract).
    _validate_drone_ranges(body)
    # 2. Custom-validated keys (regex / nested dataclass / lenient list).
    if "state_thresholds" in body and isinstance(body["state_thresholds"], dict):
        _apply_drone_state_thresholds(dc.state_thresholds, body["state_thresholds"])
    if "allowed_read_paths" in body:
        val = body["allowed_read_paths"]
        if isinstance(val, list) and all(isinstance(p, str) for p in val):
            dc.allowed_read_paths = val
    if "approval_rules" in body:
        dc.approval_rules = parse_approval_rules(body["approval_rules"])
    # 3. Generic dispatch — type validation + assignment for everything
    #    else.  New fields auto-flow.  Returns the per-section outcome
    #    that ``apply_update`` aggregates into the structured
    #    ApplyResult (Phase 7).
    return _apply_dataclass_dict(body, dc, "drones", skip_keys=_CUSTOM_KEYS)
