"""Per-section config appliers extracted from ConfigManager.

Each section's validate-and-assign logic lives in its own module here,
turned from a ``self`` method on ConfigManager into a free function of
``(cfg, body, *, deps) -> FieldOutcome``.  The :data:`SECTION_REGISTRY`
list drives ``ConfigManager.apply_update`` dispatch — adding a new
section is now a 2-file change (new module + one registry entry), no
need to remember to update the ``_KNOWN_BODY_KEYS`` allow-list
separately.

See ``docs/specs/config-manager-refactor.md`` for the extraction
spec.  The ``ApplierDeps`` bundle is the composition pattern picked
in §5 (Option A) — mirrors :class:`WorkerHealthDetectors` from the
state-tracker refactor.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

from swarm.server.config_appliers._base import ApplierDeps
from swarm.server.config_appliers.advanced import ADVANCED_KEYS, apply_advanced
from swarm.server.config_appliers.coordination import apply_coordination
from swarm.server.config_appliers.drones import apply_drones, parse_approval_rules
from swarm.server.config_appliers.jira import apply_jira
from swarm.server.config_appliers.llms import apply_llms, apply_provider_overrides
from swarm.server.config_appliers.notifications import apply_notifications
from swarm.server.config_appliers.playbooks import apply_playbooks
from swarm.server.config_appliers.queen import apply_queen
from swarm.server.config_appliers.test import apply_test
from swarm.server.config_appliers.workers import SCALARS_KEYS, apply_scalars
from swarm.server.config_appliers.workflows import apply_workflows

if TYPE_CHECKING:
    from swarm.config import HiveConfig
    from swarm.server.config_manager import FieldOutcome


class SectionApplier(Protocol):
    """A function that validates and applies one config section."""

    def __call__(
        self,
        cfg: HiveConfig,
        body: Any,
        *,
        deps: ApplierDeps,
    ) -> FieldOutcome: ...


# Order matches the existing ``apply_update`` sequence verbatim so
# legacy error precedence is preserved.  The orchestrator iterates
# this list, dispatches each applier with the right slice of the
# body, and aggregates ``FieldOutcome`` results into ``ApplyResult``.
#
# Sections marked ``merge=True`` get merged into ``result.sections``
# (visible per-section in the dashboard toast).  ``merge=False``
# sections (the void-returning llms/provider_overrides + the no-op
# workflows + the virtual advanced/scalars) only contribute to the
# top-level ``consumed`` list.
SECTION_REGISTRY: list[tuple[str, SectionApplier, bool]] = [
    ("llms", apply_llms, False),
    ("provider_overrides", apply_provider_overrides, False),
    ("drones", apply_drones, True),
    ("queen", apply_queen, True),
    ("notifications", apply_notifications, True),
    ("workflows", apply_workflows, False),
    ("test", apply_test, True),
    ("coordination", apply_coordination, True),
    ("jira", apply_jira, True),
    ("playbooks", apply_playbooks, True),
]

# Virtual sections — their keys live at the top level of the body,
# not under a section name.  Dispatched after the named sections.
VIRTUAL_APPLIERS: list[tuple[str, SectionApplier]] = [
    ("advanced", apply_advanced),
    ("scalars", apply_scalars),
]


def known_body_keys() -> frozenset[str]:
    """Derive the set of recognized top-level keys from the registries.

    Replaces the hand-maintained ``_KNOWN_BODY_KEYS`` frozenset that
    `ConfigManager` used to keep in lock-step with its dispatch chain.
    Adding a new section to ``SECTION_REGISTRY`` automatically extends
    the allow-list; the "remember to update two places" footgun is
    gone.
    """
    return frozenset(name for name, _, _ in SECTION_REGISTRY) | ADVANCED_KEYS | SCALARS_KEYS


__all__ = [
    "ADVANCED_KEYS",
    "SCALARS_KEYS",
    "SECTION_REGISTRY",
    "VIRTUAL_APPLIERS",
    "ApplierDeps",
    "SectionApplier",
    "apply_advanced",
    "apply_coordination",
    "apply_drones",
    "apply_jira",
    "apply_llms",
    "apply_notifications",
    "apply_playbooks",
    "apply_provider_overrides",
    "apply_queen",
    "apply_scalars",
    "apply_test",
    "apply_workflows",
    "known_body_keys",
    "parse_approval_rules",
]
