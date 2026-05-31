"""Cross-project task ingestion — validate and parse task files from ~/.swarm/cross-tasks/."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from swarm.tasks.task import DEPENDENCY_TYPE_MAP, PRIORITY_MAP, TYPE_MAP

CROSS_TASK_DIR = Path.home() / ".swarm" / "cross-tasks"

_MAX_TITLE_LEN = 500
_MAX_DESC_LEN = 10_000
_MAX_CRITERIA = 20
_MAX_REFS = 20


def _validate_required_string(data: dict[str, Any], field: str) -> str | None:
    """Check that *field* is a non-empty string in *data*."""
    val = data.get(field, "")
    if not val or not isinstance(val, str) or not val.strip():
        return f"{field} is required"
    return None


def _validate_enum_field(
    data: dict[str, Any], field: str, valid: dict[str, Any], default: str
) -> str | None:
    """Check that *field* (if present) is one of *valid* keys."""
    val = data.get(field, default)
    if val and val not in valid:
        return f"{field} must be one of: {', '.join(sorted(valid))}"
    return None


def _validate_list_field(data: dict[str, Any], field: str, max_len: int) -> str | None:
    """Check that *field* is a list with at most *max_len* items."""
    val = data.get(field, [])
    if not isinstance(val, list):
        return f"{field} must be a list"
    if len(val) > max_len:
        return f"{field} exceeds {max_len} items"
    return None


def validate_cross_task(data: dict[str, Any]) -> str | None:
    """Validate a cross-task payload. Returns error message or None if valid."""
    if not isinstance(data, dict):
        return "payload must be a JSON object"

    for field in ("title", "source_worker", "target_worker"):
        err = _validate_required_string(data, field)
        if err:
            return err

    title = data.get("title", "")
    if isinstance(title, str) and len(title) > _MAX_TITLE_LEN:
        return f"title exceeds {_MAX_TITLE_LEN} characters"

    for field, valid, default in [
        ("dependency_type", DEPENDENCY_TYPE_MAP, "blocks"),
        ("priority", PRIORITY_MAP, "normal"),
        ("task_type", TYPE_MAP, ""),
    ]:
        err = _validate_enum_field(data, field, valid, default)
        if err:
            return err

    desc = data.get("description", "")
    if isinstance(desc, str) and len(desc) > _MAX_DESC_LEN:
        return f"description exceeds {_MAX_DESC_LEN} characters"

    for field, max_len in [
        ("acceptance_criteria", _MAX_CRITERIA),
        ("context_refs", _MAX_REFS),
    ]:
        err = _validate_list_field(data, field, max_len)
        if err:
            return err

    return None


def parse_cross_task_file(path: Path) -> dict[str, Any] | None:
    """Read and validate a JSON cross-task file. Returns parsed dict or None."""
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    err = validate_cross_task(data)
    if err:
        return None
    return data


def scan_cross_task_dir() -> list[tuple[Path, dict[str, Any]]]:
    """Scan the cross-task directory for unprocessed .json files."""
    if not CROSS_TASK_DIR.is_dir():
        return []
    results: list[tuple[Path, dict[str, Any]]] = []
    for path in sorted(CROSS_TASK_DIR.glob("*.json")):
        data = parse_cross_task_file(path)
        if data is not None:
            results.append((path, data))
    return results
