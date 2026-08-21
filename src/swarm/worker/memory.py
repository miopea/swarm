"""Per-worker persistent memory — durable facts, preferences, and lessons."""

from __future__ import annotations

from pathlib import Path

from swarm.logging import get_logger
from swarm.paths import state_dir

_log = get_logger("worker.memory")

_MEMORY_ROOT = state_dir() / "memory"


def memory_dir(worker_name: str) -> Path:
    """Return the memory directory for a worker, creating it if needed."""
    d = _MEMORY_ROOT / worker_name
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_memory(worker_name: str) -> str:
    """Load a worker's MEMORY.md content, returning empty string if not found."""
    mem_file = memory_dir(worker_name) / "MEMORY.md"
    if not mem_file.is_file():
        return ""
    try:
        return mem_file.read_text()
    except OSError:
        _log.debug("failed to read memory for %s", worker_name)
        return ""


def save_memory(worker_name: str, content: str) -> None:
    """Save content to a worker's MEMORY.md file."""
    mem_file = memory_dir(worker_name) / "MEMORY.md"
    try:
        mem_file.write_text(content)
    except OSError:
        _log.warning("failed to save memory for %s", worker_name, exc_info=True)


def list_memory_files(worker_name: str) -> list[str]:
    """List all markdown files in a worker's memory directory."""
    d = memory_dir(worker_name)
    return sorted(p.name for p in d.glob("*.md") if p.is_file())
