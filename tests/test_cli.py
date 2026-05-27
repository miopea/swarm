"""Tests for cli.py — Click CLI commands."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from click.testing import CliRunner

from swarm.cli import _resolve_target, main
from swarm.config import GroupConfig, HiveConfig, WorkerConfig


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def sample_config():
    """Config with 2 workers and 1 group for testing."""
    return HiveConfig(
        session_name="test",
        workers=[
            WorkerConfig(name="api", path="/tmp/api"),
            WorkerConfig(name="web", path="/tmp/web"),
        ],
        groups=[GroupConfig(name="backend", workers=["api"])],
    )


@pytest.fixture
def sample_config_file(tmp_path):
    """Write a valid config file and return its path."""
    api_dir = tmp_path / "api"
    api_dir.mkdir()
    web_dir = tmp_path / "web"
    web_dir.mkdir()
    config = tmp_path / "swarm.yaml"
    config.write_text(f"""
session_name: test
workers:
  - name: api
    path: {api_dir}
  - name: web
    path: {web_dir}
groups:
  - name: backend
    workers: [api]
""")
    return str(config)


# --- Help / Version ---


def test_help(runner):
    result = runner.invoke(main, ["--help"])
    assert result.exit_code == 0
    assert "Swarm" in result.output


def test_version(runner):
    result = runner.invoke(main, ["--version"])
    assert result.exit_code == 0
    assert "version" in result.output.lower()


# --- init ---


def test_init_skip_all(runner, monkeypatch):
    """init --skip-hooks --skip-config still runs system checks."""
    monkeypatch.setattr("swarm.service.is_wsl", lambda: False)
    result = runner.invoke(main, ["init", "--skip-hooks", "--skip-config"])
    assert result.exit_code == 0
    assert "Skipping hooks" in result.output
    assert "Skipping swarm.yaml" in result.output
    assert "System readiness" in result.output


def test_init_writes_api_password(runner, monkeypatch, tmp_path):
    """init should prompt for API password and write it to config."""
    monkeypatch.setattr("swarm.service.is_wsl", lambda: False)

    # Create a fake project dir with a git repo
    project_dir = tmp_path / "projects" / "myapp"
    project_dir.mkdir(parents=True)
    (project_dir / ".git").mkdir()

    out_path = str(tmp_path / "swarm.yaml")

    # Input: "a" for all workers, "mySecret" for password, empty for domain
    result = runner.invoke(
        main,
        ["init", "--skip-hooks", "-d", str(tmp_path / "projects"), "-o", out_path],
        input="a\nmySecret\n\n",
    )
    assert result.exit_code == 0

    import yaml

    data = yaml.safe_load(Path(out_path).read_text())
    assert data["api_password"] == "mySecret"


def test_init_skips_api_password_when_empty(runner, monkeypatch, tmp_path):
    """init should omit api_password when user presses Enter (empty)."""
    monkeypatch.setattr("swarm.service.is_wsl", lambda: False)

    project_dir = tmp_path / "projects" / "myapp"
    project_dir.mkdir(parents=True)
    (project_dir / ".git").mkdir()

    out_path = str(tmp_path / "swarm.yaml")

    # Input: "a" for all workers, then empty for no password
    result = runner.invoke(
        main,
        ["init", "--skip-hooks", "-d", str(tmp_path / "projects"), "-o", out_path],
        input="a\n\n",
    )
    assert result.exit_code == 0

    import yaml

    data = yaml.safe_load(Path(out_path).read_text())
    assert "api_password" not in data


def test_init_writes_domain(runner, monkeypatch, tmp_path):
    """init should write domain when provided after password."""
    monkeypatch.setattr("swarm.service.is_wsl", lambda: False)

    project_dir = tmp_path / "projects" / "myapp"
    project_dir.mkdir(parents=True)
    (project_dir / ".git").mkdir()

    out_path = str(tmp_path / "swarm.yaml")

    # Input: "a" for all workers, "secret" for password, "swarm.example.com" for domain,
    # "n" to decline Caddy reverse proxy
    result = runner.invoke(
        main,
        ["init", "--skip-hooks", "-d", str(tmp_path / "projects"), "-o", out_path],
        input="a\nsecret\nswarm.example.com\nn\n",
    )
    assert result.exit_code == 0

    import yaml

    data = yaml.safe_load(Path(out_path).read_text())
    assert data["domain"] == "swarm.example.com"


def test_init_reverse_proxy_sets_trust_proxy(runner, monkeypatch, tmp_path):
    """init should set trust_proxy when user accepts Caddy setup."""
    monkeypatch.setattr("swarm.service.is_wsl", lambda: False)
    monkeypatch.setattr("swarm.reverse_proxy.setup_caddy", lambda domain, port=9090: True)

    project_dir = tmp_path / "projects" / "myapp"
    project_dir.mkdir(parents=True)
    (project_dir / ".git").mkdir()

    out_path = str(tmp_path / "swarm.yaml")

    # Input: "a" for all, "secret" for password, "swarm.example.com" for domain,
    # "y" to accept Caddy
    result = runner.invoke(
        main,
        ["init", "--skip-hooks", "-d", str(tmp_path / "projects"), "-o", out_path],
        input="a\nsecret\nswarm.example.com\ny\n",
    )
    assert result.exit_code == 0

    import yaml

    data = yaml.safe_load(Path(out_path).read_text())
    assert data["domain"] == "swarm.example.com"
    assert data["trust_proxy"] is True


def test_init_leaves_existing_yaml_untouched_when_db_empty(runner, monkeypatch, tmp_path):
    """When a YAML exists but the DB has no data, init must leave the
    YAML alone — the daemon will auto-migrate it on next ``swarm start``.

    Replaces the old "keep/port/fresh" prompt which was confusing
    because rules/workers actually live in the DB, not the YAML.  Any
    destructive rewrite of the YAML would break the auto-migration
    seed for first-run users.
    """
    monkeypatch.setattr("swarm.service.is_wsl", lambda: False)
    # Force the DB-state probe to report "no DB" so we hit the
    # empty-DB branch regardless of the test runner's real ~/.swarm.
    monkeypatch.setattr("swarm.cli._read_db_state", lambda: None)

    project_dir = tmp_path / "projects" / "myapp"
    project_dir.mkdir(parents=True)
    (project_dir / ".git").mkdir()

    out_path = tmp_path / "swarm.yaml"
    original_content = "# my custom config\nworkers:\n  - name: old\n    path: /old\n"
    out_path.write_text(original_content)

    args = [
        "init",
        "--skip-hooks",
        "-d",
        str(tmp_path / "projects"),
        "-o",
        str(out_path),
    ]
    # No prompt should fire — init must run end-to-end without input.
    result = runner.invoke(main, args, input="")
    assert result.exit_code == 0, result.output
    assert "migrated into swarm.db automatically" in result.output
    # YAML is untouched byte-for-byte.
    assert out_path.read_text() == original_content
    # And no backup file should be written — there's nothing to back up
    # because we're not overwriting.
    assert not (tmp_path / "swarm.yaml.bak").exists()


def test_init_skips_yaml_wizard_when_db_has_data(runner, monkeypatch, tmp_path):
    """When swarm.db already contains workers/groups/rules, init must
    NOT ask the user any config questions — their state lives in the
    DB and any YAML edit would be confusing and destructive-feeling.
    """
    monkeypatch.setattr("swarm.service.is_wsl", lambda: False)
    monkeypatch.setattr(
        "swarm.cli._read_db_state",
        lambda: {
            "path": "/home/user/.swarm/swarm.db",
            "workers": 3,
            "groups": 1,
            "global_rules": 24,
            "worker_rules": 0,
            "tasks": 0,
        },
    )

    project_dir = tmp_path / "projects" / "myapp"
    project_dir.mkdir(parents=True)
    (project_dir / ".git").mkdir()

    out_path = tmp_path / "swarm.yaml"  # file does NOT exist
    args = [
        "init",
        "--skip-hooks",
        "-d",
        str(tmp_path / "projects"),
        "-o",
        str(out_path),
    ]
    result = runner.invoke(main, args, input="")
    assert result.exit_code == 0, result.output
    # Banner mentions DB is the authoritative source...
    assert "already holds your configuration" in result.output
    # ...and no YAML was written in this case.
    assert not out_path.exists()


def test_init_wizard_runs_on_true_first_run(runner, monkeypatch, tmp_path):
    """True first run: no DB, no YAML.  The scan-and-configure wizard
    runs and produces a starter YAML that the daemon will migrate
    into swarm.db on first start.
    """
    monkeypatch.setattr("swarm.service.is_wsl", lambda: False)
    monkeypatch.setattr("swarm.cli._read_db_state", lambda: None)

    project_dir = tmp_path / "projects" / "myapp"
    project_dir.mkdir(parents=True)
    (project_dir / ".git").mkdir()

    out_path = tmp_path / "swarm.yaml"  # does not exist
    args = [
        "init",
        "--skip-hooks",
        "-d",
        str(tmp_path / "projects"),
        "-o",
        str(out_path),
    ]
    # Input: "a" for all workers, empty password (Enter to skip)
    result = runner.invoke(main, args, input="a\n\n")
    assert result.exit_code == 0, result.output
    assert out_path.exists()
    import yaml

    data = yaml.safe_load(out_path.read_text())
    assert data["workers"][0]["name"] == "myapp"


# --- _resolve_target ---


def test_resolve_target_by_group_number(sample_config):
    """Resolve target by group number (1-indexed)."""
    name, workers = _resolve_target(sample_config, "1")
    assert name == "backend"
    assert len(workers) == 1
    assert workers[0].name == "api"


def test_resolve_target_by_worker_number(sample_config):
    """Resolve target by worker number (after groups)."""
    name, workers = _resolve_target(sample_config, "2")
    assert name == "api"
    assert len(workers) == 1


def test_resolve_target_by_worker_number_second(sample_config):
    """Resolve second worker by number."""
    name, workers = _resolve_target(sample_config, "3")
    assert name == "web"
    assert len(workers) == 1


def test_resolve_target_by_group_name(sample_config):
    """Resolve target by group name."""
    name, workers = _resolve_target(sample_config, "backend")
    assert name == "backend"
    assert workers is not None


def test_resolve_target_by_worker_name(sample_config):
    """Resolve target by worker name."""
    name, workers = _resolve_target(sample_config, "web")
    assert name == "web"
    assert len(workers) == 1


def test_resolve_target_not_found(sample_config):
    """Resolve target returns None for unknown name."""
    name, workers = _resolve_target(sample_config, "nonexistent")
    assert name == "nonexistent"
    assert workers is None


def test_resolve_target_number_out_of_range(sample_config):
    """Resolve target with out-of-range number."""
    name, workers = _resolve_target(sample_config, "99")
    # Falls through to name lookup, not found
    assert workers is None


# --- validate ---


def test_validate_no_config(runner, tmp_path, monkeypatch):
    """validate should report errors when no config and no discoverable workers."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    result = runner.invoke(main, ["validate"])
    assert result.exit_code == 1
    assert "error" in result.output.lower()


def test_validate_valid_config(runner, tmp_path):
    """validate should succeed with a valid config."""
    config = tmp_path / "swarm.yaml"
    config.write_text("""
session_name: test
workers:
  - name: api
    path: /tmp
groups:
  - name: all
    workers: [api]
""")
    result = runner.invoke(main, ["validate", "-c", str(config)])
    assert result.exit_code == 0
    assert "Config OK" in result.output


def test_validate_invalid_config(runner, tmp_path):
    """validate should report errors in invalid config."""
    config = tmp_path / "swarm.yaml"
    config.write_text("""
session_name: test
workers:
  - name: api
    path: /tmp
  - name: api
    path: /tmp
""")
    result = runner.invoke(main, ["validate", "-c", str(config)])
    assert result.exit_code != 0
    assert "Duplicate worker name" in result.output


# --- tasks ---


def test_tasks_list_empty(runner, monkeypatch, tmp_path):
    """tasks list should show empty board."""
    monkeypatch.setattr("swarm.tasks.store._DEFAULT_PATH", tmp_path / "tasks.json")
    result = runner.invoke(main, ["tasks", "list"])
    assert result.exit_code == 0
    assert "No tasks" in result.output


def test_tasks_list_with_tasks(runner, monkeypatch, tmp_path):
    """tasks list should display existing tasks."""
    monkeypatch.setattr("swarm.tasks.store._DEFAULT_PATH", tmp_path / "tasks.json")
    # Create a task first
    runner.invoke(main, ["tasks", "create", "--title", "Fix bug"])
    result = runner.invoke(main, ["tasks", "list"])
    assert result.exit_code == 0
    assert "Fix bug" in result.output


def test_tasks_create(runner, monkeypatch, tmp_path):
    """tasks create should create a task."""
    monkeypatch.setattr("swarm.tasks.store._DEFAULT_PATH", tmp_path / "tasks.json")
    result = runner.invoke(main, ["tasks", "create", "--title", "Fix bug"])
    assert result.exit_code == 0
    assert "Created task" in result.output
    assert "Fix bug" in result.output


def test_tasks_create_with_priority(runner, monkeypatch, tmp_path):
    """tasks create should accept priority option."""
    monkeypatch.setattr("swarm.tasks.store._DEFAULT_PATH", tmp_path / "tasks.json")
    result = runner.invoke(main, ["tasks", "create", "--title", "Urgent fix", "--priority", "high"])
    assert result.exit_code == 0
    assert "high" in result.output


def test_tasks_create_no_title(runner):
    """tasks create without --title should fail."""
    result = runner.invoke(main, ["tasks", "create"])
    assert result.exit_code != 0


def test_tasks_assign(runner, monkeypatch, tmp_path):
    """tasks assign should assign a task to a worker."""
    monkeypatch.setattr("swarm.tasks.store._DEFAULT_PATH", tmp_path / "tasks.json")
    # Create task first
    create_result = runner.invoke(main, ["tasks", "create", "--title", "Fix bug"])
    # Extract task ID from output like "Created task [abc123]: Fix bug"
    task_id = create_result.output.split("[")[1].split("]")[0]
    result = runner.invoke(main, ["tasks", "assign", "--task-id", task_id, "--worker", "api"])
    assert result.exit_code == 0
    assert "Assigned" in result.output


def test_tasks_assign_missing_args(runner):
    """tasks assign without required args should fail."""
    result = runner.invoke(main, ["tasks", "assign"])
    assert result.exit_code != 0


def test_tasks_assign_not_found(runner, monkeypatch, tmp_path):
    """tasks assign with bad task ID should fail."""
    monkeypatch.setattr("swarm.tasks.store._DEFAULT_PATH", tmp_path / "tasks.json")
    result = runner.invoke(main, ["tasks", "assign", "--task-id", "bad", "--worker", "api"])
    assert result.exit_code != 0
    assert "not found" in result.output


def test_tasks_complete(runner, monkeypatch, tmp_path):
    """tasks complete should complete a task."""
    monkeypatch.setattr("swarm.tasks.store._DEFAULT_PATH", tmp_path / "tasks.json")
    # Create and assign task first
    create_result = runner.invoke(main, ["tasks", "create", "--title", "Fix bug"])
    task_id = create_result.output.split("[")[1].split("]")[0]
    runner.invoke(main, ["tasks", "assign", "--task-id", task_id, "--worker", "api"])
    result = runner.invoke(main, ["tasks", "complete", "--task-id", task_id])
    assert result.exit_code == 0
    assert "complete" in result.output.lower()


def test_tasks_complete_missing_id(runner, monkeypatch, tmp_path):
    """tasks complete without --task-id should fail."""
    monkeypatch.setattr("swarm.tasks.store._DEFAULT_PATH", tmp_path / "tasks.json")
    result = runner.invoke(main, ["tasks", "complete"])
    assert result.exit_code != 0


def test_tasks_complete_not_found(runner, monkeypatch, tmp_path):
    """tasks complete with bad task ID should fail."""
    monkeypatch.setattr("swarm.tasks.store._DEFAULT_PATH", tmp_path / "tasks.json")
    result = runner.invoke(main, ["tasks", "complete", "--task-id", "bad"])
    assert result.exit_code != 0
    assert "not found" in result.output


# --- status ---


def test_status_no_daemon(runner, monkeypatch):
    """status should handle no running daemon."""
    monkeypatch.setattr("swarm.cli._load_config_db_first", lambda p=None: _make_config())

    mock_get = AsyncMock(side_effect=ConnectionError("daemon not running"))
    with patch("swarm.cli._api_get", mock_get):
        result = runner.invoke(main, ["status"])
        # The status command raises SystemExit(1) on connection error
        assert result.exit_code != 0
        assert "Cannot reach daemon" in result.output


def test_status_with_workers(runner, monkeypatch):
    """status should display worker states from the daemon API."""
    monkeypatch.setattr("swarm.cli._load_config_db_first", lambda p=None: _make_config())

    mock_response = {
        "workers": [
            {"name": "api", "state": "resting", "state_duration": 10.0, "revive_count": 0},
        ]
    }

    mock_get = AsyncMock(return_value=mock_response)
    with patch("swarm.cli._api_get", mock_get):
        result = runner.invoke(main, ["status"])
        assert result.exit_code == 0
        assert "api" in result.output
        assert "resting" in result.output


def test_status_no_workers(runner, monkeypatch):
    """status should report when no workers are registered."""
    monkeypatch.setattr("swarm.cli._load_config_db_first", lambda p=None: _make_config())

    mock_get = AsyncMock(return_value={"workers": []})
    with patch("swarm.cli._api_get", mock_get):
        result = runner.invoke(main, ["status"])
        assert result.exit_code == 0
        assert "No workers" in result.output


# --- send ---


def test_send_to_worker(runner, monkeypatch):
    """send should deliver message to matching worker via daemon API."""
    monkeypatch.setattr("swarm.cli._load_config_db_first", lambda p=None: _make_config())

    mock_get = AsyncMock(return_value={"workers": [{"name": "api"}]})
    mock_post = AsyncMock(return_value={"ok": True})

    with patch("swarm.cli._api_get", mock_get), patch("swarm.cli._api_post", mock_post):
        result = runner.invoke(main, ["send", "api", "hello world"])
        assert result.exit_code == 0
        assert "Sent to api" in result.output
        assert "1 worker(s)" in result.output


def test_send_to_all(runner, monkeypatch):
    """send all should deliver to every worker via daemon API."""
    monkeypatch.setattr("swarm.cli._load_config_db_first", lambda p=None: _make_config())

    mock_get = AsyncMock(return_value={"workers": [{"name": "api"}, {"name": "web"}]})
    mock_post = AsyncMock(return_value={"ok": True})

    with patch("swarm.cli._api_get", mock_get), patch("swarm.cli._api_post", mock_post):
        result = runner.invoke(main, ["send", "all", "deploy"])
        assert result.exit_code == 0
        assert "2 worker(s)" in result.output


def test_send_to_group(runner, monkeypatch, sample_config):
    """send to a group name should deliver to all group members via daemon API."""
    monkeypatch.setattr("swarm.cli._load_config_db_first", lambda p=None: sample_config)

    mock_get = AsyncMock(return_value={"workers": [{"name": "api"}, {"name": "web"}]})
    mock_post = AsyncMock(return_value={"ok": True})

    with patch("swarm.cli._api_get", mock_get), patch("swarm.cli._api_post", mock_post):
        result = runner.invoke(main, ["send", "backend", "deploy"])
        assert result.exit_code == 0
        assert "Sent to" in result.output
        assert "1 worker(s)" in result.output


def test_send_no_matching_worker(runner, monkeypatch):
    """send to unknown worker should report no match."""
    monkeypatch.setattr("swarm.cli._load_config_db_first", lambda p=None: _make_config())

    mock_get = AsyncMock(return_value={"workers": [{"name": "api"}]})

    with patch("swarm.cli._api_get", mock_get):
        result = runner.invoke(main, ["send", "nonexistent", "hello"])
        assert result.exit_code == 0
        assert "No matching" in result.output


def test_send_no_daemon(runner, monkeypatch):
    """send with no running daemon should report error."""
    monkeypatch.setattr("swarm.cli._load_config_db_first", lambda p=None: _make_config())

    mock_get = AsyncMock(side_effect=ConnectionError("daemon not running"))

    with patch("swarm.cli._api_get", mock_get):
        result = runner.invoke(main, ["send", "api", "hello"])
        # The send command raises SystemExit(1) on connection error
        assert result.exit_code != 0
        assert "Cannot reach daemon" in result.output


# --- kill ---


def test_kill_worker(runner, monkeypatch):
    """kill should kill the named worker via daemon API."""
    monkeypatch.setattr("swarm.cli._load_config_db_first", lambda p=None: _make_config())

    mock_post = AsyncMock(return_value={"ok": True})
    with patch("swarm.cli._api_post", mock_post):
        result = runner.invoke(main, ["kill", "api"])
        assert result.exit_code == 0
        assert "Killed worker: api" in result.output


def test_kill_worker_not_found(runner, monkeypatch):
    """kill should report error for unknown worker."""
    monkeypatch.setattr("swarm.cli._load_config_db_first", lambda p=None: _make_config())

    mock_post = AsyncMock(side_effect=Exception("Worker not found"))
    with patch("swarm.cli._api_post", mock_post):
        result = runner.invoke(main, ["kill", "nonexistent"])
        # The kill command raises SystemExit(1) on failure
        assert result.exit_code != 0
        assert "Failed to kill" in result.output


def test_kill_no_daemon(runner, monkeypatch):
    """kill with no running daemon should report error."""
    monkeypatch.setattr("swarm.cli._load_config_db_first", lambda p=None: _make_config())

    mock_post = AsyncMock(side_effect=ConnectionError("daemon not running"))
    with patch("swarm.cli._api_post", mock_post):
        result = runner.invoke(main, ["kill", "api"])
        # The kill command raises SystemExit(1) on failure
        assert result.exit_code != 0
        assert "Failed to kill" in result.output


# --- stop (daemon reaper) ---


def test_stop_no_lock_file(runner, tmp_path, monkeypatch):
    """stop should report cleanly when no daemon is running."""
    monkeypatch.setattr("swarm.server.runner._DAEMON_LOCK_PATH", tmp_path / "nonexistent.lock")
    result = runner.invoke(main, ["stop"])
    assert result.exit_code == 0
    assert "No swarm daemon is running" in result.output


def test_stop_stale_lock(runner, tmp_path, monkeypatch):
    """stop should clean up a lock held by a dead PID."""
    lock = tmp_path / "daemon.lock"
    lock.write_text("99999\n")  # almost certainly dead
    monkeypatch.setattr("swarm.server.runner._DAEMON_LOCK_PATH", lock)
    monkeypatch.setattr("swarm.server.runner._pid_alive", lambda _pid: False)

    result = runner.invoke(main, ["stop"])
    assert result.exit_code == 0
    assert "Stale lock" in result.output
    assert not lock.exists()


def test_stop_live_daemon_graceful(runner, tmp_path, monkeypatch):
    """stop should SIGTERM the daemon PID and confirm shutdown."""
    lock = tmp_path / "daemon.lock"
    lock.write_text("42\n")
    monkeypatch.setattr("swarm.server.runner._DAEMON_LOCK_PATH", lock)

    # First alive check: True (before SIGTERM). Subsequent checks: False (it exited).
    alive_calls = iter([True, False])
    monkeypatch.setattr("swarm.server.runner._pid_alive", lambda _pid: next(alive_calls))

    killed: list[tuple[int, int]] = []

    def fake_kill(pid: int, sig: int) -> None:
        killed.append((pid, sig))

    monkeypatch.setattr("swarm.cli.os.kill", fake_kill)

    result = runner.invoke(main, ["stop", "--timeout", "1"])
    assert result.exit_code == 0, result.output
    assert "Stopped swarm daemon (PID 42)" in result.output
    import signal as _signal

    assert killed == [(42, _signal.SIGTERM)]


def test_stop_force_flag_sigkills_immediately(runner, tmp_path, monkeypatch):
    """--force should skip SIGTERM and send SIGKILL directly."""
    lock = tmp_path / "daemon.lock"
    lock.write_text("42\n")
    monkeypatch.setattr("swarm.server.runner._DAEMON_LOCK_PATH", lock)
    monkeypatch.setattr("swarm.server.runner._pid_alive", lambda _pid: True)

    killed: list[tuple[int, int]] = []
    monkeypatch.setattr("swarm.cli.os.kill", lambda pid, sig: killed.append((pid, sig)))

    result = runner.invoke(main, ["stop", "--force"])
    assert result.exit_code == 0
    import signal as _signal

    assert killed == [(42, _signal.SIGKILL)]
    assert "SIGKILL" in result.output


def test_stop_timeout_escalates_to_sigkill(runner, tmp_path, monkeypatch):
    """If SIGTERM doesn't take within the timeout, escalate to SIGKILL."""
    lock = tmp_path / "daemon.lock"
    lock.write_text("42\n")
    monkeypatch.setattr("swarm.server.runner._DAEMON_LOCK_PATH", lock)
    # Stay "alive" through SIGTERM + timeout loop, die only after SIGKILL.
    state = {"alive": True}
    monkeypatch.setattr("swarm.server.runner._pid_alive", lambda _pid: state["alive"])

    killed: list[tuple[int, int]] = []

    def fake_kill(pid: int, sig: int) -> None:
        import signal as _signal

        killed.append((pid, sig))
        if sig == _signal.SIGKILL:
            state["alive"] = False

    monkeypatch.setattr("swarm.cli.os.kill", fake_kill)
    # Bypass real sleeping so the test doesn't wait its full timeout.
    # stop() does `import time as _time` inside the function, so patching
    # the module-global time.sleep is what actually intercepts its calls.
    import time as _real_time

    monkeypatch.setattr(_real_time, "sleep", lambda _s: None)

    result = runner.invoke(main, ["stop", "--timeout", "0.1"])
    assert result.exit_code == 0, result.output
    import signal as _signal

    signals_sent = [s for _, s in killed]
    assert _signal.SIGTERM in signals_sent
    assert _signal.SIGKILL in signals_sent
    assert "Stopped swarm daemon" in result.output


# --- install-hooks ---


def test_install_hooks(runner):
    """install-hooks should call the install function."""
    with patch("swarm.hooks.install.install") as mock_install:
        result = runner.invoke(main, ["install-hooks"])
        assert result.exit_code == 0
        assert "Hooks installed" in result.output
        mock_install.assert_called_once_with(global_install=False)


def test_install_hooks_global(runner):
    """install-hooks --global should pass global_install=True."""
    with patch("swarm.hooks.install.install") as mock_install:
        result = runner.invoke(main, ["install-hooks", "--global"])
        assert result.exit_code == 0
        mock_install.assert_called_once_with(global_install=True)


# --- web start/stop/status ---


def test_web_start(runner, monkeypatch):
    """web start should delegate to webctl."""
    monkeypatch.setattr("swarm.cli._load_config_db_first", lambda p=None: _make_config())
    with patch("swarm.server.webctl.web_start", return_value=(True, "Started on :9090")):
        result = runner.invoke(main, ["web", "start"])
        assert result.exit_code == 0
        assert "Started" in result.output


def test_web_stop(runner):
    """web stop should delegate to webctl."""
    with patch("swarm.server.webctl.web_stop", return_value=(True, "Stopped")):
        result = runner.invoke(main, ["web", "stop"])
        assert result.exit_code == 0
        assert "Stopped" in result.output


def test_web_status_running(runner):
    """web status should show running PID."""
    with patch("swarm.server.webctl.web_is_running", return_value=12345):
        result = runner.invoke(main, ["web", "status"])
        assert result.exit_code == 0
        assert "12345" in result.output
        assert "running" in result.output.lower()


def test_web_status_not_running(runner):
    """web status should indicate when not running."""
    with patch("swarm.server.webctl.web_is_running", return_value=None):
        result = runner.invoke(main, ["web", "status"])
        assert result.exit_code == 0
        assert "not running" in result.output


# --- launch ---


def test_launch_no_args_shows_available(runner, sample_config_file):
    """launch with no args and no default_group shows available groups."""
    result = runner.invoke(main, ["launch", "-c", sample_config_file])
    assert result.exit_code == 0
    assert "Groups:" in result.output
    assert "backend" in result.output


def test_launch_all(runner, sample_config_file):
    """launch -a should launch all workers."""
    mock_post = AsyncMock(return_value={"launched": ["api", "web"]})
    with patch("swarm.cli._api_post", mock_post):
        result = runner.invoke(main, ["launch", "-a", "-c", sample_config_file])
    assert result.exit_code == 0
    assert "Launched 2 worker(s)" in result.output


def test_launch_by_group_name(runner, sample_config_file):
    """launch <group> should launch that group."""
    mock_post = AsyncMock(return_value={"launched": ["api"]})
    with patch("swarm.cli._api_post", mock_post):
        result = runner.invoke(main, ["launch", "backend", "-c", sample_config_file])
    assert result.exit_code == 0
    assert "Launched 1 worker(s)" in result.output


def test_launch_by_worker_name(runner, sample_config_file):
    """launch <worker> should launch that single worker."""
    mock_post = AsyncMock(return_value={"launched": ["web"]})
    with patch("swarm.cli._api_post", mock_post):
        result = runner.invoke(main, ["launch", "web", "-c", sample_config_file])
    assert result.exit_code == 0
    assert "Launched 1 worker(s)" in result.output


def test_launch_unknown_target(runner, sample_config_file):
    """launch <unknown> should show available groups."""
    result = runner.invoke(main, ["launch", "nonexistent", "-c", sample_config_file])
    assert result.exit_code == 0
    assert "Unknown group or worker" in result.output


def test_launch_config_errors(runner, tmp_path):
    """launch should fail if config has validation errors (duplicate worker name)."""
    config = tmp_path / "swarm.yaml"
    config.write_text(f"""
session_name: test
workers:
  - name: api
    path: {tmp_path}
  - name: api
    path: {tmp_path}
""")
    result = runner.invoke(main, ["launch", "-a", "-c", str(config)])
    assert result.exit_code != 0
    assert "Config error" in result.output


# --- launch numeric targets ---


def test_launch_by_group_number(runner, sample_config_file):
    """launch 1 should launch group at index 1 (backend)."""
    mock_post = AsyncMock(return_value={"launched": ["api"]})
    with patch("swarm.cli._api_post", mock_post):
        result = runner.invoke(main, ["launch", "1", "-c", sample_config_file])
    assert result.exit_code == 0
    assert "Launched 1 worker(s)" in result.output


def test_launch_by_worker_number(runner, sample_config_file):
    """launch 2 should launch worker at index 2 (first worker after 1 group)."""
    mock_post = AsyncMock(return_value={"launched": ["api"]})
    with patch("swarm.cli._api_post", mock_post):
        result = runner.invoke(main, ["launch", "2", "-c", sample_config_file])
    assert result.exit_code == 0
    assert "Launched 1 worker(s)" in result.output


def test_launch_number_out_of_range(runner, sample_config_file):
    """launch with out-of-range number should show available groups."""
    result = runner.invoke(main, ["launch", "99", "-c", sample_config_file])
    assert result.exit_code == 0
    assert "Unknown group or worker" in result.output


# --- launch with default_group ---


def test_launch_default_group(runner, tmp_path):
    """launch with no args should use default_group if set."""
    api_dir = tmp_path / "api"
    api_dir.mkdir()
    config = tmp_path / "swarm.yaml"
    config.write_text(f"""
session_name: test
default_group: backend
workers:
  - name: api
    path: {api_dir}
groups:
  - name: backend
    workers: [api]
""")
    mock_post = AsyncMock(return_value={"launched": ["api"]})
    with patch("swarm.cli._api_post", mock_post):
        result = runner.invoke(main, ["launch", "-c", str(config)])
    assert result.exit_code == 0
    assert "Launched 1 worker(s)" in result.output


def test_launch_default_group_not_found(runner, tmp_path):
    """launch with missing default_group should show error."""
    api_dir = tmp_path / "api"
    api_dir.mkdir()
    config = tmp_path / "swarm.yaml"
    config.write_text(f"""
session_name: test
default_group: nonexistent
workers:
  - name: api
    path: {api_dir}
groups:
  - name: backend
    workers: [api]
""")
    result = runner.invoke(main, ["launch", "-c", str(config)])
    # default_group 'nonexistent' causes config validation error
    assert result.exit_code != 0
    assert "Config error" in result.output or "Unknown" in result.output


# --- serve ---


@patch("swarm.server.daemon.run_daemon", new_callable=AsyncMock)
def test_serve_invokes_run_daemon(mock_run, runner, sample_config_file):
    """serve should call run_daemon with config."""
    result = runner.invoke(main, ["serve", "-c", sample_config_file])
    assert result.exit_code == 0


# --- daemon ---


@patch("swarm.server.daemon.run_daemon", new_callable=AsyncMock)
def test_daemon_command(mock_run, runner, sample_config_file):
    """daemon subcommand should call run_daemon."""
    result = runner.invoke(main, ["daemon", "-c", sample_config_file])
    assert result.exit_code == 0


# --- _load_config_db_first: --config bypass guard ---


def test_load_config_db_first_yaml_loaded_when_db_empty(tmp_path, monkeypatch):
    """--config <yaml> takes effect when ~/.swarm/swarm.db is absent.

    Preserves the test / fresh-install / explicit-YAML-bootstrap path
    after the Amanda 2026-05-05 fix that blocked the same flag from
    silently overriding a populated DB.
    """
    from swarm.cli import _load_config_db_first
    from swarm.db import core as db_core

    monkeypatch.setattr(db_core, "_DEFAULT_DB_PATH", tmp_path / "missing.db")

    yaml_path = tmp_path / "swarm.yaml"
    yaml_path.write_text("session_name: from-yaml\n")

    cfg = _load_config_db_first(str(yaml_path))
    assert cfg.session_name == "from-yaml"
    assert cfg.config_source == "yaml"


def test_load_config_db_first_yaml_ignored_when_db_has_data(tmp_path, monkeypatch):
    """Regression: --config <yaml> must NOT override a populated DB.

    Pre-fix, a legacy ``swarm.service`` ExecStart of
    ``swarm serve -c ~/.config/swarm/config.yaml`` carried that
    bypass through every ``os.execv`` reload.  Operators saved
    workflows / approval rules / groups via the dashboard (which
    persists to swarm.db), restarted, and the values "vanished" —
    because the YAML loader silently won and the YAML didn't have
    them.  This test locks the new behaviour: when the DB has any
    user data, the --config flag is ignored.
    """
    from swarm.cli import _load_config_db_first
    from swarm.db import core as db_core
    from swarm.db.config_store import save_config_to_db
    from swarm.db.core import SwarmDB

    db_path = tmp_path / "swarm.db"
    monkeypatch.setattr(db_core, "_DEFAULT_DB_PATH", db_path)

    # Seed the DB with user data — a single worker is enough.
    db = SwarmDB(db_path)
    seeded = HiveConfig(
        session_name="from-db",
        workers=[WorkerConfig(name="api", path=str(tmp_path))],
    )
    save_config_to_db(db, seeded)
    db.close()

    # Now write a YAML with a *different* session_name — pre-fix this
    # would silently win, post-fix the DB wins and we get a WARNING.
    yaml_path = tmp_path / "swarm.yaml"
    yaml_path.write_text("session_name: from-yaml\n")

    cfg = _load_config_db_first(str(yaml_path))
    assert cfg.session_name == "from-db"
    assert cfg.config_source == "db"


def test_strip_config_flag_handles_all_forms() -> None:
    """``-c`` / ``--config`` must be stripped from argv before os.execv.

    Pre-fix, the legacy systemd ``-c yaml`` argument propagated
    through every reload — even after the DB-first override
    gracefully ignored its value, the warning kept firing.
    """
    from swarm.server.daemon import _strip_config_flag

    # ``-c X`` (separate value)
    assert _strip_config_flag(["swarm", "serve", "-c", "/etc/swarm.yaml"]) == ["swarm", "serve"]
    # ``-cX`` (bundled)
    assert _strip_config_flag(["swarm", "-c/etc/swarm.yaml", "serve"]) == ["swarm", "serve"]
    # ``--config X``
    assert _strip_config_flag(["swarm", "--config", "/etc/swarm.yaml", "serve"]) == [
        "swarm",
        "serve",
    ]
    # ``--config=X``
    assert _strip_config_flag(["swarm", "--config=/etc/swarm.yaml", "serve"]) == ["swarm", "serve"]
    # No --config — passthrough
    assert _strip_config_flag(["swarm", "serve", "--port", "9090"]) == [
        "swarm",
        "serve",
        "--port",
        "9090",
    ]


# --- log level ---


def test_log_level_option(runner, tmp_path, monkeypatch):
    """--log-level should be accepted without error."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    result = runner.invoke(main, ["--log-level", "DEBUG", "validate"])
    # Validation may fail (no config), but --log-level should be accepted
    assert "Error: Invalid value" not in result.output


# --- check-states ---


def test_check_states_shows_workers(runner, monkeypatch):
    """check-states should show worker states from the daemon API."""
    monkeypatch.setattr("swarm.cli._load_config_db_first", lambda p=None: _make_config())

    mock_response = {
        "workers": [
            {"name": "api", "state": "resting", "state_duration": 10.5},
            {"name": "web", "state": "buzzing", "state_duration": 3.2},
        ]
    }

    mock_get = AsyncMock(return_value=mock_response)
    with patch("swarm.cli._api_get", mock_get):
        result = runner.invoke(main, ["check-states"])
        assert result.exit_code == 0
        assert "api" in result.output
        assert "resting" in result.output
        assert "10.5s" in result.output
        assert "web" in result.output
        assert "buzzing" in result.output
        assert "3.2s" in result.output


def test_check_states_no_daemon(runner, monkeypatch):
    """check-states should report error when daemon is not running."""
    monkeypatch.setattr("swarm.cli._load_config_db_first", lambda p=None: _make_config())

    mock_get = AsyncMock(side_effect=ConnectionError("daemon not running"))
    with patch("swarm.cli._api_get", mock_get):
        result = runner.invoke(main, ["check-states"])
        assert result.exit_code != 0
        assert "Cannot reach daemon" in result.output


def test_check_states_no_workers(runner, monkeypatch):
    """check-states should report when no workers are registered."""
    monkeypatch.setattr("swarm.cli._load_config_db_first", lambda p=None: _make_config())

    mock_get = AsyncMock(return_value={"workers": []})
    with patch("swarm.cli._api_get", mock_get):
        result = runner.invoke(main, ["check-states"])
        assert result.exit_code == 0
        assert "No workers" in result.output


# --- helpers ---


def _make_config():
    return HiveConfig(session_name="nonexistent")


# --- API auth token plumbing ---


def test_resolve_api_token_prefers_env_var(monkeypatch):
    """SWARM_API_PASSWORD wins over cfg.api_password."""
    from swarm.cli import _resolve_api_token

    monkeypatch.setenv("SWARM_API_PASSWORD", "from-env")
    cfg = HiveConfig(api_password="from-config")
    assert _resolve_api_token(cfg) == "from-env"


def test_resolve_api_token_falls_back_to_config(monkeypatch):
    """Without the env var, the config value is used."""
    from swarm.cli import _resolve_api_token

    monkeypatch.delenv("SWARM_API_PASSWORD", raising=False)
    cfg = HiveConfig(api_password="from-config")
    assert _resolve_api_token(cfg) == "from-config"


def test_resolve_api_token_empty_when_unset(monkeypatch):
    """No env, no config → empty string (no auth header)."""
    from swarm.cli import _resolve_api_token

    monkeypatch.delenv("SWARM_API_PASSWORD", raising=False)
    assert _resolve_api_token(HiveConfig()) == ""
    assert _resolve_api_token(None) == ""


def test_auth_headers_empty_when_no_token():
    from swarm.cli import _auth_headers

    assert _auth_headers("") == {}


def test_auth_headers_builds_bearer():
    from swarm.cli import _auth_headers

    assert _auth_headers("secret-token") == {"Authorization": "Bearer secret-token"}


def test_kill_sends_bearer_token_when_configured(runner, monkeypatch):
    """Regression: `swarm kill WORKER` must send Authorization header when
    api_password is configured, otherwise the daemon returns 401 Unauthorized
    for every command — which is exactly the bug a user reported live.
    """
    monkeypatch.delenv("SWARM_API_PASSWORD", raising=False)
    monkeypatch.setattr(
        "swarm.cli._load_config_db_first",
        lambda p=None: HiveConfig(session_name="t", api_password="cli-test-pw"),
    )

    captured: dict[str, object] = {}

    async def fake_post(port, path, json=None, *, token=""):
        captured["port"] = port
        captured["path"] = path
        captured["token"] = token
        return {"ok": True}

    with patch("swarm.cli._api_post", side_effect=fake_post):
        result = runner.invoke(main, ["kill", "api"])
    assert result.exit_code == 0, result.output
    assert captured["token"] == "cli-test-pw"
    assert captured["path"] == "/api/workers/api/kill"


def test_status_sends_bearer_token_when_configured(runner, monkeypatch):
    """`swarm status` must also send the Bearer token."""
    monkeypatch.delenv("SWARM_API_PASSWORD", raising=False)
    monkeypatch.setattr(
        "swarm.cli._load_config_db_first",
        lambda p=None: HiveConfig(session_name="t", api_password="cli-test-pw"),
    )

    captured: dict[str, object] = {}

    async def fake_get(port, path, *, token=""):
        captured["token"] = token
        return {"workers": []}

    with patch("swarm.cli._api_get", side_effect=fake_get):
        result = runner.invoke(main, ["status"])
    assert result.exit_code == 0, result.output
    assert captured["token"] == "cli-test-pw"


def test_api_get_attaches_bearer_header_to_request():
    """End-to-end: _api_get must put the token into the aiohttp request
    headers as ``Authorization: Bearer <token>``.
    """
    from swarm.cli import _api_get

    captured_headers: dict[str, str] = {}

    class _FakeResp:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def json(self):
            return {"ok": True}

        async def text(self):
            return ""

    class _FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def get(self, _url, headers=None):
            captured_headers.update(headers or {})
            return _FakeResp()

    import aiohttp

    with patch.object(aiohttp, "ClientSession", return_value=_FakeSession()):
        import asyncio

        asyncio.run(_api_get(9090, "/api/workers", token="my-token"))
    assert captured_headers.get("Authorization") == "Bearer my-token"


# ---------------------------------------------------------------------------
# swarm update — auto-restart flow
# ---------------------------------------------------------------------------


def test_restart_running_daemon_no_daemon(monkeypatch):
    """If the daemon isn't reachable, the helper reports it cleanly."""
    import asyncio

    from swarm.cli import _restart_running_daemon

    async def _fake_probe(port, token):
        return False, ""

    monkeypatch.setattr("swarm.cli._probe_daemon_sha", _fake_probe)
    result = asyncio.run(_restart_running_daemon(9090, ""))
    assert "No running daemon" in result


def test_restart_running_daemon_success(monkeypatch):
    """Reachable daemon → POST restart, poll, report new build sha."""
    import asyncio

    from swarm.cli import _restart_running_daemon

    async def _fake_probe(port, token):
        return True, "oldsha00"

    async def _fake_wait(port, token, pre_sha, timeout):
        assert pre_sha == "oldsha00"
        return "Daemon restarted with new build (newsha11)."

    # Patch both the probe and the wait helper so the core function only
    # exercises the POST path.
    monkeypatch.setattr("swarm.cli._probe_daemon_sha", _fake_probe)
    monkeypatch.setattr("swarm.cli._wait_for_daemon_sha_change", _fake_wait)

    posted = {}

    class _FakeResp:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def text(self):
            return ""

    class _FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def post(self, url, headers=None, timeout=None):
            posted["url"] = url
            posted["headers"] = headers
            return _FakeResp()

    import aiohttp

    with patch.object(aiohttp, "ClientSession", return_value=_FakeSession()):
        result = asyncio.run(_restart_running_daemon(9090, "secret"))

    assert "newsha11" in result
    assert posted["url"] == "http://localhost:9090/api/server/restart"
    assert posted["headers"]["X-Requested-With"] == "swarm-cli"
    assert posted["headers"]["Authorization"] == "Bearer secret"


def test_restart_running_daemon_post_timeout_is_ok(monkeypatch):
    """Timeout on the restart POST is expected (daemon is exiting) — still
    advance to the polling phase.
    """
    import asyncio

    from swarm.cli import _restart_running_daemon

    async def _fake_probe(port, token):
        return True, "old"

    async def _fake_wait(port, token, pre_sha, timeout):
        return "Daemon is back up."

    monkeypatch.setattr("swarm.cli._probe_daemon_sha", _fake_probe)
    monkeypatch.setattr("swarm.cli._wait_for_daemon_sha_change", _fake_wait)

    class _FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def post(self, url, headers=None, timeout=None):
            raise TimeoutError("simulated shutdown mid-request")

    import aiohttp

    with patch.object(aiohttp, "ClientSession", return_value=_FakeSession()):
        result = asyncio.run(_restart_running_daemon(9090, ""))

    assert "back up" in result


def test_update_command_no_restart_flag(runner, monkeypatch):
    """--no-restart skips the auto-restart step entirely."""
    from swarm.update import UpdateResult

    async def _fake_check(force=False):
        return UpdateResult(
            current_version="2026.4.5.20",
            remote_version="2026.4.5.21",
            available=True,
            commit_sha="abc1234",
            commit_message="fix bug",
            error=None,
        )

    async def _fake_perform(on_output=None):
        return True, "ok"

    restart_called = {"hit": False}

    async def _fake_restart(port, token):
        restart_called["hit"] = True
        return "should not be called"

    monkeypatch.setattr("swarm.update.check_for_update", _fake_check)
    monkeypatch.setattr("swarm.update.perform_update", _fake_perform)
    monkeypatch.setattr("swarm.cli._restart_running_daemon", _fake_restart)

    result = runner.invoke(main, ["update", "--no-restart"], input="y\n")
    assert result.exit_code == 0, result.output
    assert restart_called["hit"] is False
    assert "Restart any running swarm processes" in result.output


def test_update_command_triggers_restart(runner, monkeypatch):
    """Default path: after install, auto-restart is attempted."""
    from swarm.update import UpdateResult

    async def _fake_check(force=False):
        return UpdateResult(
            current_version="2026.4.5.20",
            remote_version="2026.4.5.21",
            available=True,
            commit_sha="abc1234",
            commit_message="fix bug",
            error=None,
        )

    async def _fake_perform(on_output=None):
        return True, "ok"

    async def _fake_restart(port, token):
        return "Daemon restarted with new build (newsha)."

    monkeypatch.setattr("swarm.update.check_for_update", _fake_check)
    monkeypatch.setattr("swarm.update.perform_update", _fake_perform)
    monkeypatch.setattr("swarm.cli._restart_running_daemon", _fake_restart)

    result = runner.invoke(main, ["update"], input="y\n")
    assert result.exit_code == 0, result.output
    assert "Restarting running daemon" in result.output
    assert "Daemon restarted" in result.output


def test_update_command_restart_no_daemon(runner, monkeypatch):
    """When there's no running daemon, the CLI still exits cleanly."""
    from swarm.update import UpdateResult

    async def _fake_check(force=False):
        return UpdateResult(
            current_version="2026.4.5.20",
            remote_version="2026.4.5.21",
            available=True,
            commit_sha="abc1234",
            commit_message="fix bug",
            error=None,
        )

    async def _fake_perform(on_output=None):
        return True, "ok"

    async def _fake_restart(port, token):
        return "No running daemon detected — changes will apply next time swarm starts."

    monkeypatch.setattr("swarm.update.check_for_update", _fake_check)
    monkeypatch.setattr("swarm.update.perform_update", _fake_perform)
    monkeypatch.setattr("swarm.cli._restart_running_daemon", _fake_restart)

    result = runner.invoke(main, ["update"], input="y\n")
    assert result.exit_code == 0, result.output
    assert "No running daemon detected" in result.output
