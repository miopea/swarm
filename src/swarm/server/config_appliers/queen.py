"""``queen`` section applier — interactive queen tuning + oversight."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from swarm.config.models import OversightConfig, QueenConfig
from swarm.server.config_manager import FieldOutcome, _apply_dataclass_dict

if TYPE_CHECKING:
    from swarm.config import HiveConfig
    from swarm.server.config_appliers._base import ApplierDeps


# (key, type_check, coerce_fn | None, error_message, constraint | None)
_QUEEN_FIELDS: tuple[tuple[str, tuple[type, ...], Any, str, Any], ...] = (
    ("cooldown", (int, float), float, "must be a non-negative number", lambda v: v >= 0),
    ("enabled", (bool,), None, "must be boolean", None),
    ("system_prompt", (str,), None, "must be a string", None),
    (
        "min_confidence",
        (int, float),
        float,
        "must be a number between 0.0 and 1.0",
        lambda v: 0.0 <= v <= 1.0,
    ),
    ("max_session_calls", (int,), None, "must be >= 1", lambda v: v >= 1),
    ("max_session_age", (int, float), float, "must be > 0", lambda v: v > 0),
    ("auto_assign_tasks", (bool,), None, "must be boolean", None),
    (
        "queen_thread_retention_days",
        (int,),
        None,
        "must be >= 0 (0 = keep forever)",
        lambda v: v >= 0,
    ),
)


# Queen keys handled by custom validators in ``_apply_queen_scalars``
# and ``_apply_queen_oversight``.  Generic dispatch skips these and
# only fires for unknown sub-keys (drift detection).
_CUSTOM_KEYS: frozenset[str] = frozenset(
    {
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
)


def _apply_queen_oversight(cfg_ov: OversightConfig, ov: dict[str, Any]) -> None:
    """Validate and apply queen.oversight sub-section."""
    if "enabled" in ov:
        if not isinstance(ov["enabled"], bool):
            raise ValueError("queen.oversight.enabled must be boolean")
        cfg_ov.enabled = ov["enabled"]
    for k in ("buzzing_threshold_minutes", "drift_check_interval_minutes"):
        if k in ov:
            v = ov[k]
            if not isinstance(v, (int, float)) or v <= 0:
                raise ValueError(f"queen.oversight.{k} must be > 0")
            setattr(cfg_ov, k, float(v))
    if "max_calls_per_hour" in ov:
        v = ov["max_calls_per_hour"]
        if not isinstance(v, int) or v < 1:
            raise ValueError("queen.oversight.max_calls_per_hour must be >= 1")
        cfg_ov.max_calls_per_hour = v
    if "operator_engagement_minutes" in ov:
        v = ov["operator_engagement_minutes"]
        if not isinstance(v, (int, float)) or v < 0:
            raise ValueError("queen.oversight.operator_engagement_minutes must be >= 0")
        cfg_ov.operator_engagement_minutes = float(v)


def _apply_queen_scalars(cfg_q: QueenConfig, qn: dict[str, Any]) -> None:
    """Validate and apply flat queen fields."""
    for key, types, coerce, msg, check in _QUEEN_FIELDS:
        if key not in qn:
            continue
        val = qn[key]
        if not isinstance(val, types):
            raise ValueError(f"queen.{key} {msg}")
        if check and not check(val):
            raise ValueError(f"queen.{key} {msg}")
        setattr(cfg_q, key, coerce(val) if coerce else val)


def apply_queen(
    cfg: HiveConfig,
    body: dict[str, Any],
    *,
    deps: ApplierDeps,  # protocol-uniform; queen doesn't use it
) -> FieldOutcome:
    """Validate and apply the ``queen`` section of a config update.

    ``_QUEEN_FIELDS`` covers the primitive scalars with
    range-check semantics; ``oversight`` is a nested dataclass
    validated by ``_apply_queen_oversight``.  Generic dispatch runs as
    a final pass to surface any unknown sub-key as a WARNING (Phase 3
    drift detection) and to populate the structured ApplyResult
    (Phase 7).
    """
    qc = cfg.queen
    # Track what the bespoke validators consumed by hand so the outcome
    # reflects the full apply, not just dispatch's tail.
    consumed_custom: list[str] = []
    for key in _CUSTOM_KEYS:
        if key in body:
            consumed_custom.append(key)
    _apply_queen_scalars(qc, body)
    if "oversight" in body and isinstance(body["oversight"], dict):
        _apply_queen_oversight(qc.oversight, body["oversight"])
    outcome = _apply_dataclass_dict(body, qc, "queen", skip_keys=_CUSTOM_KEYS)
    outcome.consumed.extend(consumed_custom)
    return outcome
