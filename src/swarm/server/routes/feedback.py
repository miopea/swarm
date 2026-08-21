"""Feedback routes — bug reports, feature requests, questions.

Captures diagnostics locally, redacts them, and either:
1. Submits directly via the user's ``gh`` CLI (preferred — full body,
   no size limit, posts as the user's GitHub account), or
2. Builds a pre-filled GitHub issue URL for the browser fallback.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import cast

from aiohttp import web

from swarm.feedback import build_issue_url, collect_attachments
from swarm.feedback.builder import Attachment as BuilderAttachment
from swarm.feedback.builder import Category, FeedbackPayload, build_markdown
from swarm.feedback.gh_submit import GhSubmitError, check_gh_status, submit_via_gh
from swarm.paths import state_dir
from swarm.server.helpers import get_daemon, handle_errors, json_error

_LAST_REPORT_PATH = state_dir() / "last-report.json"

_VALID_CATEGORIES: set[str] = {"bug", "feature", "question"}

# Bug reports get all attachments on by default. Feature requests and
# questions only need environment + install ID unless the user opts in.
_DEFAULT_ENABLED: dict[str, set[str]] = {
    "bug": {"environment", "install_id", "logs", "drone_events", "config"},
    "feature": {"environment", "install_id"},
    "question": {"environment", "install_id"},
}


def register(app: web.Application) -> None:
    app.router.add_post("/api/feedback/preview", handle_preview)
    app.router.add_post("/api/feedback/build-url", handle_build_url)
    app.router.add_post("/api/feedback/build-markdown", handle_build_markdown)
    app.router.add_post("/api/feedback/submit", handle_submit_via_gh)
    app.router.add_get("/api/feedback/gh-status", handle_gh_status)
    app.router.add_post("/api/feedback/save", handle_save)
    app.router.add_get("/api/feedback/last", handle_last)


def _validate_category(raw: object) -> Category:
    if not isinstance(raw, str) or raw not in _VALID_CATEGORIES:
        return "bug"
    return cast(Category, raw)


@handle_errors
async def handle_preview(request: web.Request) -> web.Response:
    """Return the default collected + redacted payload for the modal.

    Request body: ``{"category": "bug" | "feature" | "question"}``
    """
    try:
        body = await request.json()
    except json.JSONDecodeError:
        body = {}
    category = _validate_category(body.get("category"))

    daemon = get_daemon(request)
    attachments = collect_attachments(daemon)

    enabled_keys = _DEFAULT_ENABLED.get(category, _DEFAULT_ENABLED["bug"])
    return web.json_response(
        {
            "category": category,
            "attachments": [
                {
                    "key": a.key,
                    "label": a.label,
                    "content": a.content,
                    "redacted_count": a.redacted_count,
                    "enabled": a.key in enabled_keys,
                }
                for a in attachments
            ],
        }
    )


def _parse_payload(body: dict[str, object]) -> FeedbackPayload | web.Response:
    title_raw = body.get("title", "")
    description_raw = body.get("description", "")
    if not isinstance(title_raw, str) or not title_raw.strip():
        return json_error("Title is required")
    if len(title_raw) > 200:
        return json_error("Title too long (max 200 characters)")
    if not isinstance(description_raw, str):
        description_raw = ""
    if len(description_raw) > 20_000:
        return json_error("Description too long (max 20000 characters)")

    category = _validate_category(body.get("category"))

    atts_raw = body.get("attachments", [])
    if not isinstance(atts_raw, list):
        return json_error("attachments must be a list")

    attachments: list[BuilderAttachment] = []
    for item in atts_raw:
        if not isinstance(item, dict):
            continue
        key = item.get("key", "")
        label = item.get("label", "")
        content = item.get("content", "")
        enabled = bool(item.get("enabled", True))
        if not isinstance(key, str) or not isinstance(label, str) or not isinstance(content, str):
            continue
        attachments.append(
            BuilderAttachment(key=key, label=label, content=content, enabled=enabled)
        )

    return FeedbackPayload(
        title=title_raw.strip(),
        description=description_raw,
        category=category,
        attachments=attachments,
    )


@handle_errors
async def handle_build_url(request: web.Request) -> web.Response:
    """Build the GitHub issue URL + full markdown from an edited payload."""
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return json_error("Invalid JSON body")

    parsed = _parse_payload(body)
    if isinstance(parsed, web.Response):
        return parsed

    url, markdown, truncated = build_issue_url(parsed)
    return web.json_response(
        {
            "url": url,
            "markdown": markdown,
            "truncated": truncated,
        }
    )


@handle_errors
async def handle_build_markdown(request: web.Request) -> web.Response:
    """Return just the assembled markdown body (no URL, no truncation).

    Used by the preview-and-confirm step: the client sends the current
    payload, the server renders the full markdown, and the client shows
    it in an editable textarea before the user clicks Confirm.
    """
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return json_error("Invalid JSON body")

    parsed = _parse_payload(body)
    if isinstance(parsed, web.Response):
        return parsed

    return web.json_response({"markdown": build_markdown(parsed)})


def _payload_to_dict(payload: FeedbackPayload) -> dict[str, object]:
    return {
        "title": payload.title,
        "description": payload.description,
        "category": payload.category,
        "attachments": [asdict(a) for a in payload.attachments],
    }


@handle_errors
async def handle_save(request: web.Request) -> web.Response:
    """Persist the last-submitted payload to ~/.swarm/last-report.json."""
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return json_error("Invalid JSON body")

    parsed = _parse_payload(body)
    if isinstance(parsed, web.Response):
        return parsed

    data = _payload_to_dict(parsed)
    data["markdown"] = build_markdown(parsed)

    try:
        _LAST_REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
        _LAST_REPORT_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError as e:
        return json_error(f"Could not save last report: {e}", status=500)

    return web.json_response({"saved": True})


@handle_errors
async def handle_last(request: web.Request) -> web.Response:
    del request  # aiohttp requires the parameter but we don't use it
    """Return the last-saved payload, or an empty stub if none exists."""
    if not _LAST_REPORT_PATH.exists():
        return web.json_response({"exists": False})
    try:
        data = json.loads(_LAST_REPORT_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return web.json_response({"exists": False})
    return web.json_response({"exists": True, "payload": data})


@handle_errors
async def handle_gh_status(request: web.Request) -> web.Response:
    """Return whether ``gh`` is installed and authenticated.

    The dashboard uses this to decide whether to show the "Submit via
    gh" primary button or fall back to the URL-prefill flow.
    """
    del request
    status = await check_gh_status()
    return web.json_response(
        {
            "installed": status.installed,
            "authenticated": status.authenticated,
            "account": status.account,
            "error": status.error,
        }
    )


@handle_errors
async def handle_submit_via_gh(request: web.Request) -> web.Response:
    """Create a GitHub issue directly via the user's ``gh`` CLI.

    Body is piped on stdin so there is no URL-length limit. The issue
    is created under the authenticated gh user's identity.
    """
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return json_error("Invalid JSON body")

    parsed = _parse_payload(body)
    if isinstance(parsed, web.Response):
        return parsed

    # If the client provides a body_override, use it verbatim — this
    # supports the "preview and edit the final markdown" flow where the
    # user has already seen the assembled body and may have edited it.
    override = body.get("body_override")
    if isinstance(override, str) and override.strip():
        if len(override) > 65_000:
            return json_error("Body too long (max 65000 characters)")
        markdown = override
    else:
        markdown = build_markdown(parsed)

    try:
        result = await submit_via_gh(
            title=parsed.title,
            body=markdown,
            category=parsed.category,
        )
    except GhSubmitError as e:
        return json_error(f"gh submission failed: {e}", status=502)

    # Best-effort save
    data = _payload_to_dict(parsed)
    data["markdown"] = markdown
    data["submitted_url"] = result.url
    try:
        _LAST_REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
        _LAST_REPORT_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError:
        pass

    return web.json_response({"url": result.url})
