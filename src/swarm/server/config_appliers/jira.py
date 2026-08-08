"""``jira`` section applier — Jira sync config."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from swarm.server.config_manager import FieldOutcome, _resolve_hints, _warn_unknown_subkeys

if TYPE_CHECKING:
    from swarm.config import HiveConfig
    from swarm.server.config_appliers._base import ApplierDeps


_JIRA_STRING_KEYS: tuple[str, ...] = (
    "project",
    "client_id",
    "client_secret",
    "cloud_id",
)


def _apply_jira_strings(cfg: object, jr: dict[str, Any], keys: tuple[str, ...]) -> None:
    """Validate and apply string fields on the Jira config."""
    for key in keys:
        if key in jr:
            if not isinstance(jr[key], str):
                raise ValueError(f"jira.{key} must be a string")
            val = jr[key].strip()
            if not val and key in ("client_id", "client_secret"):
                continue
            setattr(cfg, key, val)


def _apply_jira_v2_fields(jc: Any, body: dict[str, Any], consumed: list[str]) -> None:
    """Apply the v2 list/dict fields (projects, issue types, per-project maps).

    Extracted to keep apply_jira under the complexity gate. Without these the UI's
    projects box silently never saved: the applier consumed nothing, so the value
    round-tripped back to the old one and the operator watched their input revert with
    no error at all. Confirmations were lost the same way.
    """
    if "projects" in body:
        val = body["projects"]
        if not isinstance(val, list):
            raise ValueError("jira.projects must be a list")
        jc.projects = [str(x).strip() for x in val if str(x).strip()]
        consumed.append("projects")
    if "issue_types" in body:
        val = body["issue_types"]
        if not isinstance(val, list):
            raise ValueError("jira.issue_types must be a list")
        jc.issue_types = [str(x).strip() for x in val if str(x).strip()]
        consumed.append("issue_types")
    if "project_status_maps" in body:
        val = body["project_status_maps"]
        if not isinstance(val, dict):
            raise ValueError("jira.project_status_maps must be an object")
        jc.project_status_maps = {
            str(k): {str(sk): str(sv) for sk, sv in (v or {}).items()} for k, v in val.items()
        }
        consumed.append("project_status_maps")
    if "confirmed_projects" in body:
        val = body["confirmed_projects"]
        if not isinstance(val, list):
            raise ValueError("jira.confirmed_projects must be a list")
        jc.confirmed_projects = [str(x).strip() for x in val if str(x).strip()]
        consumed.append("confirmed_projects")


def apply_jira(
    cfg: HiveConfig,
    body: dict[str, Any],
    *,
    deps: ApplierDeps,  # protocol-uniform; jira doesn't use it
) -> FieldOutcome:
    """Validate and apply the ``jira`` section of a config update.

    Every JiraConfig field has bespoke validation (regex-shape
    client_id, range-checked sync_interval, per-project status maps,
    empty-string fallbacks for credentials) so the body of this
    handler stays hand-coded.  Phase 7 instruments it to track
    consumed keys and emit the standard unknown-sub-key WARNING via
    the generic dispatch sweep.
    """
    from swarm.config import JiraConfig

    jc = cfg.jira
    consumed: list[str] = []
    if "enabled" in body:
        if not isinstance(body["enabled"], bool):
            raise ValueError("jira.enabled must be boolean")
        jc.enabled = body["enabled"]
        consumed.append("enabled")
    for key in _JIRA_STRING_KEYS:
        if key in body:
            consumed.append(key)
    _apply_jira_strings(jc, body, _JIRA_STRING_KEYS)
    if "sync_interval_minutes" in body:
        val = body["sync_interval_minutes"]
        if not isinstance(val, (int, float)) or val <= 0:
            raise ValueError("jira.sync_interval_minutes must be > 0")
        jc.sync_interval_minutes = float(val)
        consumed.append("sync_interval_minutes")
    _apply_jira_v2_fields(jc, body, consumed)
    # Drift sweep — every JiraConfig field is custom-handled above, so
    # dispatch only fires for unknown sub-keys.
    outcome = FieldOutcome(consumed=list(consumed))
    _warn_unknown_subkeys(body, JiraConfig, "jira")
    # Compute unknown via the same field set the warn helper uses.
    declared = set(_resolve_hints(JiraConfig).keys())
    outcome.unknown = sorted(set(body) - declared)
    return outcome
