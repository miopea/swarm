"""TestConfig — configuration for test mode runs."""

from __future__ import annotations

import hashlib
import os
from dataclasses import asdict, dataclass, field

from swarm.paths import state_path_str

_DEFAULT_REPORT_DIR = state_path_str("reports")


@dataclass
class TestConfig:
    """Settings for ``swarm test`` supervised orchestration testing."""

    enabled: bool = False
    port: int = 9091  # dedicated test port (separate from main web UI)
    auto_resolve_delay: float = 4.0  # seconds before Queen resolves proposal
    report_dir: str = _DEFAULT_REPORT_DIR
    auto_complete_min_idle: float = 10.0  # shorter idle threshold for test mode
    # Optional override — pin test runs to a specific model identifier so
    # results stay comparable across model upgrades. Falls back to the
    # provider's default when empty.
    pin_model: str = ""


# Env vars that meaningfully influence worker behaviour — hashed into the
# snapshot so infra differences show up in the report without leaking
# secrets. Values that look sensitive are excluded.
_TRACKED_ENV_VARS = (
    "CLAUDE_MODEL",
    "ANTHROPIC_MODEL",
    "CLAUDE_CODE_MODEL",
    "CLAUDE_PROJECT_DIR",
    "SWARM_PROVIDER",
    "SWARM_WORKER_COUNT",
)


@dataclass
class InfraSnapshot:
    """Infrastructure fingerprint captured at test-run start.

    Anthropic's *Quantifying infrastructure noise* post shows that infra
    configuration can swing benchmarks more than model differences.
    Pinning these values to every run log makes later regressions
    debuggable instead of mysterious.
    """

    model: str = ""
    provider: str = ""
    worker_count: int = 0
    port: int = 0
    claude_home: str = ""
    swarm_version: str = ""
    python_version: str = ""
    platform: str = ""
    env_hash: str = ""
    env_keys: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def compute_env_hash(env: dict[str, str] | None = None) -> tuple[str, list[str]]:
    """Hash the values of ``_TRACKED_ENV_VARS`` — for drift detection only.

    Returns ``(hex_digest, sorted_keys_present)``. The digest is a
    stable fingerprint of env vars that meaningfully alter worker
    behaviour; values are *not* stored, only their hash, so secrets
    never leak into the test report.
    """
    source = env if env is not None else os.environ
    present: list[tuple[str, str]] = []
    for name in _TRACKED_ENV_VARS:
        value = source.get(name)
        if value:
            present.append((name, value))
    present.sort()
    if not present:
        return "", []
    payload = "\n".join(f"{k}={v}" for k, v in present).encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()[:12]
    return digest, [k for k, _ in present]
