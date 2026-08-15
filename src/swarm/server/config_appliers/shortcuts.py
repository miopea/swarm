"""``shortcuts`` section applier — operator-defined PTY key sequences (#1677)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from swarm.config.models import WorkerShortcut
from swarm.server.config_manager import FieldOutcome

if TYPE_CHECKING:
    from swarm.config import HiveConfig
    from swarm.server.config_appliers._base import ApplierDeps


def apply_shortcuts(
    cfg: HiveConfig,
    body: object,
    *,
    deps: ApplierDeps,  # protocol-uniform; shortcuts doesn't use it
) -> FieldOutcome:
    """Apply the ``shortcuts`` list from a config update.

    WITHOUT THIS APPLIER THE FEATURE IS INERT FROM THE OPERATOR'S SIDE. The model,
    serializer and loader all handled `shortcuts`, and a shortcut written into swarm.yaml
    by hand worked — but `PUT /api/config`, which is what the config page actually calls,
    reported ``unknown: ["shortcuts"]`` and dropped it. Measured live before this existed:
    the PUT returned 200 with `shortcuts: []`, so the save LOOKED successful and changed
    nothing. That is the Amanda-class "I typed it but it didn't save" symptom the
    fail-loud guard in ``apply_update`` was built to surface, and it surfaced this.

    Entries need BOTH a label and keys. A half-entry would render a button in the shortcut
    bar that writes nothing — worse than an absent one, because it looks like it works.

    Unlike ``workflows``, an empty list IS honoured: removing your last shortcut has to be
    possible from the same surface that added it, and there is no separate clear endpoint.
    The destructive-empty-overwrite footgun that guard exists for does not apply here —
    the dashboard does not send a `shortcuts` key unless the operator edited the list.
    """
    outcome = FieldOutcome()
    if not isinstance(body, list):
        outcome.unknown.append("shortcuts")
        return outcome

    parsed: list[WorkerShortcut] = []
    for entry in body:
        if not isinstance(entry, dict):
            continue
        label = str(entry.get("label", "")).strip()
        keys = str(entry.get("keys", ""))
        if not label or not keys:
            continue
        parsed.append(WorkerShortcut(label=label, keys=keys))

    cfg.shortcuts = parsed
    outcome.consumed.append("shortcuts")
    return outcome
