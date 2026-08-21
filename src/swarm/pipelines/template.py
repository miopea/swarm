"""Pipeline template loader — creates pipelines from YAML template files."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from swarm.logging import get_logger
from swarm.paths import state_dir
from swarm.pipelines.models import Pipeline, PipelineStep, StepType

_log = get_logger("pipelines.template")

_DEFAULT_TEMPLATE_DIR = state_dir() / "pipeline-templates"


def load_template(
    template_name: str,
    template_dir: str | None = None,
) -> Pipeline:
    """Load a pipeline template YAML and return a Pipeline instance.

    Raises ``FileNotFoundError`` if the template doesn't exist.
    Raises ``ValueError`` on invalid template content.
    """
    import yaml

    base = Path(template_dir) if template_dir else _DEFAULT_TEMPLATE_DIR
    path = base / f"{template_name}.yaml"
    if not path.exists():
        path = base / f"{template_name}.yml"
    if not path.exists():
        raise FileNotFoundError(f"Pipeline template not found: {template_name} (searched {base})")

    data = yaml.safe_load(path.read_text())
    if not isinstance(data, dict):
        raise ValueError(f"Template {template_name} must be a YAML mapping")

    return _build_pipeline(data, template_name)


def _build_pipeline(data: dict[str, Any], template_name: str) -> Pipeline:
    name = data.get("name", template_name)
    steps_data = data.get("steps", [])
    if not isinstance(steps_data, list):
        raise ValueError("Template 'steps' must be a list")

    steps: list[PipelineStep] = []
    for sd in steps_data:
        step = PipelineStep(
            id=sd["id"],
            name=sd.get("name", sd["id"]),
            step_type=StepType(sd.get("type", "agent")),
            description=sd.get("description", ""),
            depends_on=sd.get("depends_on", []),
            task_type=sd.get("task_type", "chore"),
            assigned_worker=sd.get("assigned_worker"),
            service=sd.get("service", ""),
            config=sd.get("config", {}),
            schedule=sd.get("schedule", ""),
        )
        steps.append(step)

    return Pipeline(
        name=name,
        description=data.get("description", ""),
        steps=steps,
        template_name=template_name,
        tags=data.get("tags", []),
    )
