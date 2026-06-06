"""Tests for the feedback collector."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from pathlib import Path

from swarm.feedback.collector import _tail_file, collect_attachments
from swarm.feedback.install_id import get_install_id


def test_tail_file_returns_last_n_lines(tmp_path):
    p = tmp_path / "sample.log"
    p.write_text("\n".join(f"line{i}" for i in range(100)) + "\n")
    out = _tail_file(p, 5)
    lines = out.strip().splitlines()
    assert lines == ["line95", "line96", "line97", "line98", "line99"]


def test_tail_file_missing_file(tmp_path):
    p = tmp_path / "nope.log"
    assert _tail_file(p, 10) == ""


def test_get_install_id_creates_and_persists(tmp_path):
    target = tmp_path / "install-id"
    first = get_install_id(target)
    assert target.exists()
    # Validates UUID shape
    uuid.UUID(first)
    # Second call returns the same value
    second = get_install_id(target)
    assert first == second


def test_get_install_id_ignores_blank_file(tmp_path):
    target = tmp_path / "install-id"
    target.write_text("   \n")
    new_id = get_install_id(target)
    assert new_id.strip()
    uuid.UUID(new_id)


def test_collect_attachments_with_no_daemon(tmp_path, monkeypatch):
    # Point HOME somewhere empty so there's no real swarm.log
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    result = collect_attachments(None)
    keys = {a.key for a in result}
    assert keys == {"environment", "install_id", "logs", "drone_events", "config"}
    env = next(a for a in result if a.key == "environment")
    assert "Swarm" in env.content
    assert "Python" in env.content


def test_collect_attachments_reads_log(tmp_path, monkeypatch):
    swarm_dir = tmp_path / ".swarm"
    swarm_dir.mkdir()
    log_path = swarm_dir / "swarm.log"
    log_path.write_text("2024-01-01 ERROR something bad\n" * 10)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    # Reimport to pick up new Path.home
    from swarm.feedback import collector as collector_mod

    monkeypatch.setattr(collector_mod, "_DEFAULT_LOG_PATH", log_path)
    result = collect_attachments(None)
    logs = next(a for a in result if a.key == "logs")
    assert "ERROR something bad" in logs.content


# Minimal dataclass stand-ins for HiveConfig. The real HiveConfig lives in
# swarm.db and is held in memory on the daemon — the collector just needs
# *a* dataclass it can asdict().
@dataclass
class _FakeWorker:
    name: str = ""
    password: str = ""


@dataclass
class _FakeJira:
    url: str = ""
    client_secret: str = ""
    api_token: str = ""


@dataclass
class _FakeConfig:
    log_level: str = "INFO"
    jira: _FakeJira = field(default_factory=_FakeJira)
    workers: list[_FakeWorker] = field(default_factory=list)


class _FakeDaemon:
    def __init__(self, config):
        self.config = config


def test_collect_attachments_config_redacts_secrets(tmp_path, monkeypatch):
    config = _FakeConfig(
        log_level="INFO",
        jira=_FakeJira(
            url="https://example.atlassian.net",
            client_secret="super-secret-value",
            api_token="token-abc",
        ),
        workers=[_FakeWorker(name="worker1", password="plaintext-pw")],
    )
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    result = collect_attachments(_FakeDaemon(config))
    attachment = next(a for a in result if a.key == "config")
    assert "super-secret-value" not in attachment.content
    assert "plaintext-pw" not in attachment.content
    assert "token-abc" not in attachment.content
    assert "<redacted>" in attachment.content
    assert "https://example.atlassian.net" in attachment.content
    assert attachment.label == "Configuration (redacted)"
    assert attachment.redacted_count >= 3


def test_collect_attachments_config_with_no_daemon(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    result = collect_attachments(None)
    attachment = next(a for a in result if a.key == "config")
    assert "no daemon" in attachment.content.lower()


def test_config_env_refs_extracts_dollar_var_names():
    """#feedback-audit A: scan the config for $VAR references so their live
    values can be scrubbed from logs/events."""
    from swarm.feedback.collector import _config_env_refs

    config = _FakeConfig(jira=_FakeJira(client_secret="$JIRA_SECRET", api_token="literal"))
    refs = _config_env_refs(_FakeDaemon(config))
    assert "JIRA_SECRET" in refs
    assert "literal" not in refs  # only $-prefixed values count


def test_collect_attachments_scrubs_env_referenced_secret_from_logs(tmp_path, monkeypatch):
    """#feedback-audit A: a secret set via an env var the config references
    ($VAR) is scrubbed out of the LOGS attachment — the env-value scrub layer
    is now wired through collect_attachments (was dormant)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("FEEDBACK_TEST_SECRET", "env-secret-value-123456")
    config = _FakeConfig(jira=_FakeJira(client_secret="$FEEDBACK_TEST_SECRET"))
    log = tmp_path / "swarm.log"
    log.write_text("worker connected with env-secret-value-123456 ok\n")

    result = collect_attachments(_FakeDaemon(config), log_path=log)
    logs = next(a for a in result if a.key == "logs")
    assert "env-secret-value-123456" not in logs.content
    assert "<env-secret>" in logs.content
