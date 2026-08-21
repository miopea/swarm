"""Tests for testing/config.py and config.py TestConfig integration."""

from pathlib import Path

import yaml

from swarm.config import HiveConfig, TestConfig, _parse_config, serialize_config
from swarm.paths import state_path_str
from swarm.testing.config import TestConfig as TestingTestConfig


def _write_yaml(tmp_path: Path, data: dict) -> Path:
    p = tmp_path / "swarm.yaml"
    p.write_text(yaml.dump(data, default_flow_style=False))
    return p


class TestTestConfig:
    def test_defaults(self):
        cfg = TestConfig()
        assert cfg.enabled is False
        assert cfg.auto_resolve_delay == 4.0
        # Follows the state dir rather than naming ~/.swarm outright, so a
        # relocated install writes reports beside its own state.
        assert cfg.report_dir == state_path_str("reports")
        assert cfg.report_dir.startswith("~/")

    def test_testing_module_matches(self):
        """TestConfig in config.py and testing/config.py should have same fields."""
        cfg1 = TestConfig()
        cfg2 = TestingTestConfig()
        assert cfg1.enabled == cfg2.enabled
        assert cfg1.auto_resolve_delay == cfg2.auto_resolve_delay
        assert cfg1.report_dir == cfg2.report_dir
        assert cfg1.auto_complete_min_idle == cfg2.auto_complete_min_idle


class TestHiveConfigWithTest:
    def test_default_test_config(self):
        cfg = HiveConfig()
        assert cfg.test.enabled is False
        assert cfg.test.auto_resolve_delay == 4.0

    def test_parse_test_section(self, tmp_path):
        data = {
            "test": {
                "enabled": True,
                "auto_resolve_delay": 2.0,
                "report_dir": "/tmp/reports",
            }
        }
        path = _write_yaml(tmp_path, data)
        cfg = _parse_config(path)
        assert cfg.test.enabled is True
        assert cfg.test.auto_resolve_delay == 2.0
        assert cfg.test.report_dir == "/tmp/reports"

    def test_parse_empty_test_section(self, tmp_path):
        data = {"test": {}}
        path = _write_yaml(tmp_path, data)
        cfg = _parse_config(path)
        assert cfg.test.enabled is False
        assert cfg.test.auto_resolve_delay == 4.0

    def test_parse_no_test_section(self, tmp_path):
        data = {"session_name": "test"}
        path = _write_yaml(tmp_path, data)
        cfg = _parse_config(path)
        assert cfg.test.enabled is False

    def test_serialize_always_includes_test(self):
        """test section is always included (templates access unconditionally)."""
        cfg = HiveConfig()
        data = serialize_config(cfg)
        assert "test" in data
        assert data["test"]["port"] == 9091

    def test_serialize_non_default_includes_test(self):
        cfg = HiveConfig()
        cfg.test.enabled = True
        data = serialize_config(cfg)
        assert "test" in data
        assert data["test"]["enabled"] is True
