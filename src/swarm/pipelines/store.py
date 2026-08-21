"""Pipeline persistence — save/load pipeline state to disk."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from swarm.logging import get_logger
from swarm.paths import state_dir
from swarm.pipelines.models import Pipeline, pipeline_from_dict

_log = get_logger("pipelines.store")

_DEFAULT_PATH = state_dir() / "pipelines.json"


class PipelineStore:
    """Persist pipelines as JSON to a file (follows FileTaskStore pattern)."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or _DEFAULT_PATH
        self._lock = threading.Lock()

    def save(self, pipelines: dict[str, Pipeline]) -> None:
        """Write all pipelines to disk atomically."""
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            data = [p.to_dict() for p in pipelines.values()]
            try:
                tmp = self.path.with_suffix(f".tmp.{os.getpid()}")
                tmp.write_text(json.dumps(data, indent=2))
                os.replace(tmp, self.path)
            except OSError:
                _log.warning("failed to save pipelines to %s", self.path, exc_info=True)

    def load(self) -> dict[str, Pipeline]:
        """Read pipelines from disk. Returns empty dict if file missing or corrupt."""
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text())
            if not isinstance(data, list):
                _log.warning("pipelines file %s does not contain a list", self.path)
                return {}
            pipelines: dict[str, Pipeline] = {}
            for item in data:
                pipeline = pipeline_from_dict(item)
                pipelines[pipeline.id] = pipeline
            _log.info("loaded %d pipelines from %s", len(pipelines), self.path)
            return pipelines
        except (json.JSONDecodeError, OSError, KeyError, TypeError, ValueError):
            _log.warning("failed to load pipelines from %s", self.path, exc_info=True)
            return {}
