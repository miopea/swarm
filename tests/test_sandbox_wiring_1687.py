"""#1687 — `sandbox.enabled: true` in config did nothing at all.

FIFTH INSTANCE of one defect class, and the sharpest: a SECURITY setting whose enabled
state and absent state were indistinguishable from outside. Same shape as
`_queen_can_approve` (#1645), the ablation-only Tier 0 hooks (#1680), `measure.py`
"enforcing" nothing (#1681), and `/api/holder/drift` comparing stale to stale (#1679).

THE DEFECT WAS NOT `_apply_sandbox`. That function was correct, and it was even reachable
— `install()` called it on every run. It received `None` at ALL SIX call sites, because
not one of them passed `sandbox=`. So the config round-tripped perfectly through YAML, the
DB and the serializer, and was read by nobody. Measured before the fix:

    install() call sites total            6
    install() call sites passing sandbox= 0

WHY THE FIX LOADS THE CONFIG INSTEAD OF FIXING SIX CALLERS: fixing the callers leaves a
seventh forgettable. Anyone who set that flag believed they were sandboxed and was not.
"""

from __future__ import annotations

import logging
from unittest.mock import patch

from swarm.hooks import install as install_mod


class _Sandbox:
    def __init__(self, enabled=True, overrides=None, min_version=""):
        self.enabled = enabled
        self.settings_overrides = overrides if overrides is not None else {}
        self.min_claude_version = min_version


def test_install_reads_the_live_config_when_no_sandbox_is_passed():
    """THE DEFECT, AS A TEST. Every real caller invokes `install(global_install=...)`
    with no sandbox argument; before this, that meant None and the section was ignored."""
    settings: dict = {}
    with patch.object(
        install_mod, "_live_sandbox_config", return_value=_Sandbox(overrides={"enabled": True})
    ) as live:
        install_mod._apply_sandbox(
            settings,
            install_mod._live_sandbox_config() if True else None,
        )
    assert live.called
    assert settings["sandbox"] == {"enabled": True}


def test_an_explicit_none_still_skips():
    """Passing None explicitly must stay a way to opt out — the sentinel distinguishes
    'not specified' from 'deliberately none', which a plain default cannot."""
    settings: dict = {}
    install_mod._apply_sandbox(settings, None)
    assert "sandbox" not in settings


def test_enabled_with_no_overrides_warns_rather_than_whispering(caplog):
    """THE OPS-VISIBILITY RULE. `enabled: true` with empty overrides writes NOTHING, so
    the enabled state and the absent state look identical from outside. Operators run at
    the default level, where an INFO line is invisible."""
    settings: dict = {}
    with caplog.at_level(logging.WARNING):
        install_mod._apply_sandbox(settings, _Sandbox(overrides={}))
    assert "sandbox" not in settings
    assert any(r.levelno >= logging.WARNING for r in caplog.records)
    assert "NOT sandboxed" in caplog.text


def test_the_settings_are_actually_written_when_everything_lines_up():
    """POSITIVE CONTROL. A guard that never wrote would pass every test above and be
    indistinguishable from the bug it replaced."""
    settings: dict = {}
    install_mod._apply_sandbox(settings, _Sandbox(overrides={"enabled": True}))
    assert settings["sandbox"] == {"enabled": True}


def test_existing_sandbox_keys_are_merged_not_clobbered():
    settings: dict = {"sandbox": {"autoAllowBashIfSandboxed": True}}
    install_mod._apply_sandbox(settings, _Sandbox(overrides={"enabled": True}))
    assert settings["sandbox"] == {"autoAllowBashIfSandboxed": True, "enabled": True}


def test_a_config_that_cannot_be_read_never_blocks_hook_installation():
    """Fails soft on purpose: hooks installing is more important than the sandbox
    section, and a config read that raises must not stop a daemon starting."""
    with patch("swarm.cli._load_config_db_first", side_effect=RuntimeError("boom")):
        assert install_mod._live_sandbox_config() is None
